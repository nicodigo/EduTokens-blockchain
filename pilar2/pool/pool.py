"""Pool Coordinator — fan-in for a local cluster of workers.

Consumes mining tasks from the NCT, partitions the nonce space across
its local workers, verifies their results, and forwards valid solutions
back to the NCT.

Worker count is determined dynamically from heartbeats; ``POOL_WORKER_COUNT``
is used as a fallback when no heartbeat data is available yet.

A background monitor thread detects workers that die mid-mining and
re-publishes orphaned sub-ranges without waiting for the NCT timeout.

Usage::

    python -m pool.pool
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI

from broker.broker import (
    CONTROL_ROUTING_KEY,
    EXCHANGE,
    RESULTS_QUEUE,
    broadcast_abort,
    declare_topology,
    get_connection,
    is_recoverable_rabbitmq_error,
    persistent_props,
    publish_tasks,
    reconnect_rabbitmq,
)
from broker.messages import ControlMessage, ResultMessage, TaskMessage
from shared.block import Block
from shared.env import env_int, env_float
from shared.schemas import HealthResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_WORKER_COUNT = 2
DEFAULT_NONCE_SPACE = 1_000_000_000
DEFAULT_HEALTH_PORT = 8090
DEFAULT_HEARTBEAT_TIMEOUT = 15.0
DEFAULT_MONITOR_INTERVAL = 5.0        # seconds between dead-worker checks
DEFAULT_RESULT_TIMEOUT = 60.0         # seconds before logging slow-mining warning

# ---------------------------------------------------------------------------
# PoolCoordinator
# ---------------------------------------------------------------------------


class PoolCoordinator:
    """Coordinates a local pool of workers that collaborate on mining.

    Each pool binds its own inbox queue to ``task.mining`` so every
    pool receives a copy of the NCT's broadcast.  The pool then
    partitions the nonce space among its workers.

    Worker count is driven by heartbeats — the pool listens on
    ``worker.{pool_id}.*`` and counts workers seen in the last
    ``heartbeat_timeout`` seconds.  Falls back to ``worker_count``
    when no heartbeat data is available (e.g. at startup).
    """

    def __init__(
        self,
        pool_id: str,
        rmq_url: str,
        worker_count: int = DEFAULT_WORKER_COUNT,
        health_port: int = DEFAULT_HEALTH_PORT,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> None:
        self.pool_id = pool_id
        self.rmq_url = rmq_url
        self._worker_count_fallback = worker_count
        self.health_port = health_port

        # Current mining context
        self._current_block_index: Optional[int] = None
        self._current_fingerprint: str = ""
        self._current_difficulty: int = 0
        self._current_task_id: str = ""
        self._current_nonce_space: int = DEFAULT_NONCE_SPACE

        # Worker heartbeat tracking (dynamic worker count)
        self._heartbeat_lock = threading.Lock()

        # Mining context lock (audit M2: prevents races between
        # _on_mining_task, _on_worker_result, and _monitor_loop)
        self._mining_lock = threading.Lock()
        self._heartbeat_timeout = heartbeat_timeout
        self._worker_heartbeats: dict[str, float] = {}
        self._ready_workers: set[str] = set()  # workers that have sent ≥1 heartbeat post-init
        self._registered_workers: set[str] = set()  # audit M3: workers that sent registration

        # Dead-worker monitor (audit H1: generation counter prevents TOCTOU race)
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_active: threading.Event = threading.Event()
        self._monitor_generation: int = 0
        self._original_worker_count: int = 0
        self._monitor_interval = env_float("POOL_MONITOR_INTERVAL", DEFAULT_MONITOR_INTERVAL)
        self._result_timeout = env_float("POOL_RESULT_TIMEOUT", DEFAULT_RESULT_TIMEOUT)

        self._shutdown: threading.Event = threading.Event()
        self._channel: Any = None

        # -- prometheus counters (best-effort, no lock needed) --
        self.start_time = time.time()
        self.tasks_received_total: int = 0
        self.tasks_completed_total: int = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        conn = get_connection(url=self.rmq_url)
        self._channel = conn.channel()

        # NCT-side topology (results queue, worker registry)
        declare_topology(self._channel)

        # Pool inbox — receives mining tasks from NCT (fanout)
        inbox = f"pool.{self.pool_id}.inbox"
        self._channel.queue_declare(queue=inbox, durable=True)
        self._channel.queue_bind(exchange=EXCHANGE, queue=inbox, routing_key="task.mining")

        # Pool internal queues for workers
        tasks_q = f"pool.{self.pool_id}.tasks"
        results_q = f"pool.{self.pool_id}.results"
        self._channel.queue_declare(queue=tasks_q, durable=True)
        self._channel.queue_declare(queue=results_q, durable=True)
        self._channel.queue_bind(exchange=EXCHANGE, queue=tasks_q,
                                 routing_key=f"pool.{self.pool_id}.task.*")
        self._channel.queue_bind(exchange=EXCHANGE, queue=results_q,
                                 routing_key=f"pool.{self.pool_id}.result.*")

        # Worker heartbeat registry — used for dynamic worker count
        registry_q = f"pool.{self.pool_id}.registry"
        self._channel.queue_declare(queue=registry_q, durable=True)
        self._channel.queue_bind(exchange=EXCHANGE, queue=registry_q,
                                 routing_key=f"pool-worker.{self.pool_id}.*")

        # Worker registration queue (audit M3) — separate from heartbeats
        # so the pool knows exactly which workers to expect before the
        # first heartbeat arrives.
        reg_q = f"pool.{self.pool_id}.registrations"
        self._channel.queue_declare(queue=reg_q, durable=True)
        self._channel.queue_bind(exchange=EXCHANGE, queue=reg_q,
                                 routing_key=f"pool.{self.pool_id}.register")

        # Health HTTP server
        threading.Thread(target=self._run_health, daemon=True, name="health").start()

        # NCT heartbeat — register this pool as a "worker" so the NCT
        # knows the pool is alive (audit H2 corrected: pool IS the worker
        # from the NCT's perspective)
        self._nct_heartbeat_interval = env_float("POOL_NCT_HEARTBEAT_INTERVAL", 30.0)
        threading.Thread(target=self._nct_heartbeat_loop,
                         daemon=True, name="nct-heartbeat").start()

        # Consumers (routed by pika to the correct callback)
        self._channel.basic_qos(prefetch_count=1)
        self._channel.basic_consume(queue=inbox, on_message_callback=self._on_mining_task,
                                     auto_ack=False)
        self._channel.basic_consume(queue=results_q, on_message_callback=self._on_worker_result,
                                     auto_ack=False)
        self._channel.basic_consume(queue=registry_q, on_message_callback=self._on_worker_heartbeat,
                                     auto_ack=True)
        self._channel.basic_consume(queue=reg_q, on_message_callback=self._on_worker_registration,
                                     auto_ack=True)

        logger.info("Pool %s ready (fallback_workers=%d, heartbeat_timeout=%.0fs) — health on :%d",
                     self.pool_id, self._worker_count_fallback,
                     self._heartbeat_timeout, self.health_port)

        # Blocking consume with automatic reconnect (audit H2)
        _consume_with_reconnect(self)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._monitor_active.clear()
        if self._channel is not None:
            try:
                self._channel.stop_consuming()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Worker heartbeat tracking
    # ------------------------------------------------------------------

    def _on_worker_heartbeat(self, _ch: Any, _method: Any, _props: Any, body: bytes) -> None:
        data = json.loads(body.decode())
        with self._heartbeat_lock:
            # Always use pool's clock — ignore worker timestamp (clocks may skew)
            self._worker_heartbeats[data["worker_id"]] = time.time()
            self._ready_workers.add(data["worker_id"])

    # ------------------------------------------------------------------
    # Worker registration (audit M3)
    # ------------------------------------------------------------------

    def _on_worker_registration(self, _ch: Any, _method: Any, _props: Any, body: bytes) -> None:
        """Record a worker as explicitly registered with this pool.

        Audit M3: this closes the "dynamic worker count starts at 0" gap.
        Before the first heartbeat, the pool knows which workers to expect
        because they sent a RegistrationMessage at startup.
        """
        data = json.loads(body.decode())
        wid = data.get("worker_id", "unknown")
        with self._heartbeat_lock:
            self._registered_workers.add(wid)
        logger.info("Pool %s: worker %s registered", self.pool_id, wid)

    def _get_active_worker_count(self) -> int:
        """Return number of ready workers that sent a heartbeat recently.

        Audit M3: only counts workers that are both registered AND have a
        recent heartbeat.  Unregistered workers sending heartbeats are ignored.
        """
        cutoff = time.time() - self._heartbeat_timeout
        with self._heartbeat_lock:
            stale = [
                wid for wid, ts in self._worker_heartbeats.items() if ts < cutoff
            ]
            for wid in stale:
                del self._worker_heartbeats[wid]
                self._ready_workers.discard(wid)
            # Count workers that are alive AND ready (initialised).
            # Audit M3: if registration messages HAVE been received, only count
            # workers that explicitly registered.  When no registrations have
            # arrived yet (e.g. solo workers, startup before reg message),
            # fall back to the old behaviour of counting all ready workers.
            if self._registered_workers:
                candidates = [
                    wid for wid in self._worker_heartbeats
                    if wid in self._ready_workers and wid in self._registered_workers
                ]
            else:
                candidates = [
                    wid for wid in self._worker_heartbeats
                    if wid in self._ready_workers
                ]
            return len(candidates)

    # ------------------------------------------------------------------
    # Mining task → partition & distribute
    # ------------------------------------------------------------------

    def _on_mining_task(self, ch: Any, method: Any, _props: Any, body: bytes) -> None:
        """Consume a mining task published by the NCT and fan it out to workers.

        **Competitive pool architecture (audit H3):** the NCT publishes one
        ``TaskMessage`` to the ``task.mining`` topic exchange.  Every pool
        subscribed to this exchange receives a copy.  Pools then compete:
        each partitions the *full* nonce space among its own workers, and
        the first pool to find a valid PoW wins.  With N pools, (N-1)/N
        of the total GPU compute is redundant work — an accepted
        trade-off in this PoC for implementation simplicity.

        The ``active_pools`` field on the NCT ``/status`` endpoint reports
        how many pools are currently competing, making the redundancy
        observable.
        """
        task = TaskMessage.from_json(body.decode())
        logger.info("Received mining task for block %d (range=[%d, %d])",
                     task.block_index, task.range_min, task.range_max)

        count = self._get_active_worker_count()
        if count == 0:
            # No heartbeats seen yet — fall back to static config
            count = self._worker_count_fallback
            if count == 0:
                logger.warning("No active workers and fallback is 0 — skipping block %d",
                               task.block_index)
                # Signal NCT so it doesn't wait uselessly
                self._channel.basic_publish(
                    exchange=EXCHANGE,
                    routing_key=f"pool.{self.pool_id}.status",
                    body=json.dumps({
                        "worker_id": self.pool_id,
                        "action": "pool_no_workers",
                        "block_index": task.block_index,
                        "timestamp": time.time(),
                    }, sort_keys=True),
                    properties=persistent_props(),
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
            logger.info("No heartbeats yet; using fallback worker_count=%d", count)

        with self._mining_lock:
            self._current_block_index = task.block_index
            self._current_fingerprint = task.fingerprint
            self._current_difficulty = task.difficulty
            self._current_task_id = task.task_id
            self._current_nonce_space = task.range_max - task.range_min + 1

        self.tasks_received_total += 1
        publish_tasks(
            self._channel,
            block_index=task.block_index,
            fingerprint=task.fingerprint,
            difficulty=task.difficulty,
            num_workers=count,
            range_size=task.range_max - task.range_min + 1,
            routing_key_prefix=f"pool.{self.pool_id}.task",
        )

        # Start dead-worker monitor — always fresh per task (audit H1)
        self._original_worker_count = count
        self._monitor_generation += 1
        gen = self._monitor_generation
        self._monitor_active.set()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(gen,), daemon=True, name="monitor",
        )
        self._monitor_thread.start()

        ch.basic_ack(delivery_tag=method.delivery_tag)

    # ------------------------------------------------------------------
    # Worker result → verify → forward to NCT
    # ------------------------------------------------------------------

    def _on_worker_result(self, ch: Any, method: Any, _props: Any, body: bytes) -> None:
        # Parse the incoming message first — malformed bodies must be
        # nacked so they don't block the queue (audit H2).
        try:
            result = ResultMessage.from_json(body.decode())
        except Exception:
            logger.warning(
                "Pool %s: malformed worker result (not valid JSON) — dropped",
                self.pool_id,
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        # M2: snapshot mining context under lock so the monitor thread
        # cannot observe a half-written state.
        with self._mining_lock:
            current_block = self._current_block_index
            current_fingerprint = self._current_fingerprint
            current_difficulty = self._current_difficulty

        # Stale check — block already mined, but message was processed
        if current_block is None or result.block_index != current_block:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Verify PoW locally before forwarding (canonical check via Block)
        valid, pow_hash = Block.verify_result(
            current_fingerprint,
            current_difficulty,
            result.nonce,
            result.hash,
        )
        if not valid:
            logger.warning(
                "Pool %s: invalid PoW from %s (hash=%s) — dropped",
                self.pool_id, result.worker_id, pow_hash,
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        # Forward valid solution to NCT — if this fails, nack so the
        # result can be retried (audit H2).
        try:
            self._channel.basic_publish(
                exchange=EXCHANGE,
                routing_key=f"result.{self.pool_id}",
                body=result.to_json(),
                properties=persistent_props(),
            )
        except Exception:
            logger.exception(
                "Pool %s: failed to forward result to NCT — requeuing",
                self.pool_id,
            )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        logger.info("Pool %s: valid nonce %d from %s — forwarded to NCT",
                     self.pool_id, result.nonce, result.worker_id)

        # Ack the worker's result now that it's safely forwarded
        ch.basic_ack(delivery_tag=method.delivery_tag)
        self.tasks_completed_total += 1

        # M2: clear mining context under lock only if the block hasn't
        # changed since we took the snapshot (prevents wiping a new task).
        with self._mining_lock:
            if self._current_block_index == current_block:
                self._monitor_active.clear()
                self._current_block_index = None
                self._current_fingerprint = ""
                self._current_difficulty = 0
                self._current_task_id = ""

    # ------------------------------------------------------------------
    # Abort
    # ------------------------------------------------------------------

    def _broadcast_abort(self, task_id: str, channel: Any = None) -> None:
        """Broadcast an abort signal.  Uses *channel* if given, else self._channel."""
        ch = channel if channel is not None else self._channel
        msg = ControlMessage(action="abort", task_id=task_id)
        ch.basic_publish(
            exchange=EXCHANGE,
            routing_key=f"pool.{self.pool_id}.control",
            body=msg.to_json(),
            properties=persistent_props(),
            mandatory=True,
        )
        logger.info("Pool %s: broadcast abort for task %s", self.pool_id, task_id)

    # ------------------------------------------------------------------
    # Dead-worker monitor
    # ------------------------------------------------------------------

    def _monitor_loop(self, generation: int) -> None:
        """Periodically check if workers died mid-mining and re-publish if so.

        Only reacts when the active worker count drops below what it was
        when the task was published — this indicates a worker died and its
        sub-range is orphaned.  Does NOT republish when workers are alive
        but slow; the NCT timeout handles that case.

        *generation* is the value of ``_monitor_generation`` at the time
        this monitor was started.  If it no longer matches the current
        generation (because a new task arrived and a fresher monitor
        replaced this one), the loop exits immediately — closing the
        TOCTOU race documented in audit H1.

        Uses its OWN connection — pika.BlockingConnection is not thread-safe
        and the main thread is blocked in start_consuming().
        """
        # Own connection so we don't corrupt the main consume thread's AMQP stream.
        # Also disable AMQP heartbeat — this loop sleeps between iterations.
        _mon_url = self.rmq_url + ("&" if "?" in self.rmq_url else "?") + "heartbeat=0"
        mon_conn = get_connection(url=_mon_url)
        mon_ch = mon_conn.channel()

        start = time.time()
        while self._monitor_active.is_set() and self._monitor_generation == generation:
            self._monitor_active.wait(timeout=self._monitor_interval)
            if not self._monitor_active.is_set():
                break
            if self._monitor_generation != generation:
                break  # superseded by a newer monitor (audit H1)

            # M2: snapshot mining context under lock for this iteration.
            with self._mining_lock:
                current_block = self._current_block_index
                current_fingerprint = self._current_fingerprint
                current_difficulty = self._current_difficulty
                current_nonce_space = self._current_nonce_space
                current_task_id = self._current_task_id

            # Already forwarded a result for this block?
            if current_block is None:
                break

            active = self._get_active_worker_count()
            elapsed = time.time() - start

            if active < self._original_worker_count:
                logger.warning(
                    "Worker(s) died mid-mining: %d→%d active. "
                    "Re-publishing for block %d.",
                    self._original_worker_count, active,
                    current_block,
                )
                # M1: abort surviving workers before republishing so they
                # discard their old (now-overlapping) sub-ranges cleanly.
                if active > 0:
                    self._broadcast_abort(current_task_id, channel=mon_ch)
                if active > 0 or self._worker_count_fallback > 0:
                    count = active if active > 0 else self._worker_count_fallback
                    publish_tasks(
                        mon_ch,
                        block_index=current_block,
                        fingerprint=current_fingerprint,
                        difficulty=current_difficulty,
                        num_workers=count,
                        range_size=current_nonce_space,
                        routing_key_prefix=f"pool.{self.pool_id}.task",
                    )
                    self._original_worker_count = count
                else:
                    # No workers left at all — signal NCT
                    mon_ch.basic_publish(
                        exchange=EXCHANGE,
                        routing_key=f"pool.{self.pool_id}.status",
                        body=json.dumps({
                            "worker_id": self.pool_id,
                            "action": "pool_no_workers",
                            "block_index": current_block,
                            "timestamp": time.time(),
                        }, sort_keys=True),
                        properties=persistent_props(),
                    )
                    break  # no workers left, stop monitoring

            elif elapsed > self._result_timeout:
                logger.warning(
                    "Block %d: no result after %.0fs with %d workers still alive. "
                    "Waiting for NCT timeout + range expansion.",
                    current_block, elapsed, active,
                )

        # Close the monitor's connection cleanly
        try:
            mon_conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def _run_health(self) -> None:
        app = FastAPI(title=f"Pool {self.pool_id}", version="1.0.0")

        @app.get("/health", response_model=HealthResponse)
        def health() -> HealthResponse:
            return HealthResponse(status="ok")

        @app.get("/metrics")
        def metrics():
            """Prometheus text format metrics endpoint."""
            from fastapi.responses import Response

            uptime = time.time() - self.start_time
            workers = len(self._worker_heartbeats)
            return Response(
                content=(
                    "# HELP pool_uptime_seconds Uptime of the pool process\n"
                    "# TYPE pool_uptime_seconds gauge\n"
                    f"pool_uptime_seconds {uptime:.2f}\n"
                    "# HELP pool_tasks_received_total Mining tasks received\n"
                    "# TYPE pool_tasks_received_total counter\n"
                    f"pool_tasks_received_total {self.tasks_received_total}\n"
                    "# HELP pool_tasks_completed_total Results forwarded to NCT\n"
                    "# TYPE pool_tasks_completed_total counter\n"
                    f"pool_tasks_completed_total {self.tasks_completed_total}\n"
                    "# HELP pool_active_workers Workers seen via heartbeat\n"
                    "# TYPE pool_active_workers gauge\n"
                    f"pool_active_workers {workers}\n"
                ),
                media_type="text/plain; charset=utf-8",
            )

        uvicorn.run(app, host="0.0.0.0", port=self.health_port, log_level="info")

    def _nct_heartbeat_loop(self) -> None:
        """Periodically publish a heartbeat to the NCT so it tracks this
        pool as a worker entity (audit H2 corrected).

        Uses routing key ``worker.{pool_id}`` so the NCT's
        ``worker_registry`` queue receives it via the ``worker.#`` binding.

        Uses its OWN connection — pika.BlockingConnection is not thread-safe
        and the main thread is blocked in start_consuming().
        """
        hb_conn: Any = None
        hb_ch: Any = None
        # Disable AMQP heartbeat — this loop sleeps 30 s between iterations
        # so protocol-level heartbeats would always time out.
        _hb_url = self.rmq_url + ("&" if "?" in self.rmq_url else "?") + "heartbeat=0"

        while not self._shutdown.wait(timeout=self._nct_heartbeat_interval):
            try:
                if hb_conn is None or not hb_conn.is_open \
                        or hb_ch is None or not hb_ch.is_open:
                    hb_conn = get_connection(url=_hb_url)
                    hb_ch = hb_conn.channel()
                hb_ch.basic_publish(
                    exchange=EXCHANGE,
                    routing_key=f"worker.{self.pool_id}",
                    body=json.dumps({
                        "worker_id": self.pool_id,
                        "role": "pool",
                        "timestamp": time.time(),
                    }, sort_keys=True),
                    properties=persistent_props(),
                )
            except Exception:
                logger.debug(
                    "Pool %s NCT heartbeat failed — reconnecting",
                    self.pool_id,
                )
                hb_ch = None
                hb_conn = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# Reconnection helpers (audit H2) — module level so they survive reconnects
# ---------------------------------------------------------------------------


def _setup_pool_topology(channel: Any, pool_id: str) -> None:
    """Re-declare pool-specific queues and bindings on *channel*."""
    inbox = f"pool.{pool_id}.inbox"
    channel.queue_declare(queue=inbox, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=inbox, routing_key="task.mining")

    tasks_q = f"pool.{pool_id}.tasks"
    results_q = f"pool.{pool_id}.results"
    channel.queue_declare(queue=tasks_q, durable=True)
    channel.queue_declare(queue=results_q, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=tasks_q,
                       routing_key=f"pool.{pool_id}.task.*")
    channel.queue_bind(exchange=EXCHANGE, queue=results_q,
                       routing_key=f"pool.{pool_id}.result.*")

    registry_q = f"pool.{pool_id}.registry"
    channel.queue_declare(queue=registry_q, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=registry_q,
                       routing_key=f"pool-worker.{pool_id}.*")

    reg_q = f"pool.{pool_id}.registrations"
    channel.queue_declare(queue=reg_q, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=reg_q,
                       routing_key=f"pool.{pool_id}.register")


def _consume_with_reconnect(pool: Any) -> None:
    """Blocking consume loop that reconnects on RabbitMQ failure."""
    while not pool._shutdown.is_set():
        try:
            pool._channel.start_consuming()
        except Exception as exc:
            if pool._shutdown.is_set():
                break
            if not is_recoverable_rabbitmq_error(exc):
                logger.exception("Unrecoverable RabbitMQ error — exiting")
                raise
            logger.warning(
                "Pool %s lost RabbitMQ connection: %s. Reconnecting…",
                pool.pool_id, exc,
            )
            try:
                new_conn, new_ch = reconnect_rabbitmq(pool.rmq_url)
                _setup_pool_topology(new_ch, pool.pool_id)
                pool._channel = new_ch
                pool._channel.basic_qos(prefetch_count=1)
                pool._channel.basic_consume(
                    queue=f"pool.{pool.pool_id}.inbox",
                    on_message_callback=pool._on_mining_task,
                    auto_ack=False,
                )
                pool._channel.basic_consume(
                    queue=f"pool.{pool.pool_id}.results",
                    on_message_callback=pool._on_worker_result,
                    auto_ack=False,
                )
                pool._channel.basic_consume(
                    queue=f"pool.{pool.pool_id}.registry",
                    on_message_callback=pool._on_worker_heartbeat,
                    auto_ack=True,
                )
                pool._channel.basic_consume(
                    queue=f"pool.{pool.pool_id}.registrations",
                    on_message_callback=pool._on_worker_registration,
                    auto_ack=True,
                )
                logger.info("Pool %s reconnected and re-subscribed", pool.pool_id)
            except Exception as reconnect_exc:
                logger.error("Pool %s reconnect failed: %s", pool.pool_id, reconnect_exc)
                time.sleep(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log_file = os.getenv("LOG_FILE")
    setup_logging(log_file)

    pool_id = os.getenv("POOL_ID", f"pool-{uuid.uuid4().hex[:6]}")
    rmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    worker_count = env_int("POOL_WORKER_COUNT", DEFAULT_WORKER_COUNT)
    health_port = env_int("HEALTH_PORT", DEFAULT_HEALTH_PORT)

    coordinator = PoolCoordinator(
        pool_id=pool_id,
        rmq_url=rmq_url,
        worker_count=worker_count,
        health_port=health_port,
    )

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        coordinator.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    coordinator.run()


if __name__ == "__main__":
    main()
