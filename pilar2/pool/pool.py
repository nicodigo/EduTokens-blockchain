"""Pool Coordinator — fan-in for a local cluster of workers.

Consumes mining tasks from the NCT, partitions the nonce space across
its local workers, verifies their results, and forwards valid solutions
back to the NCT.

Worker count is determined dynamically from heartbeats; ``POOL_WORKER_COUNT``
is used as a fallback when no heartbeat data is available yet.

Usage::

    python -m pool.pool
"""

from __future__ import annotations

import hashlib
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
    publish_tasks,
)
from broker.messages import ControlMessage, ResultMessage, TaskMessage
from shared.schemas import HealthResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_WORKER_COUNT = 2
DEFAULT_NONCE_SPACE = 1_000_000_000
DEFAULT_HEALTH_PORT = 8090
DEFAULT_HEARTBEAT_TIMEOUT = 15.0

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

        # Worker heartbeat tracking (dynamic worker count)
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_timeout = heartbeat_timeout
        self._worker_heartbeats: dict[str, float] = {}

        self._shutdown: threading.Event = threading.Event()
        self._channel: Any = None
        self.start_time: float = 0.0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.start_time = time.time()
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
                                 routing_key=f"worker.{self.pool_id}.*")

        # Health HTTP server
        threading.Thread(target=self._run_health, daemon=True, name="health").start()

        # Consumers (routed by pika to the correct callback)
        self._channel.basic_qos(prefetch_count=1)
        self._channel.basic_consume(queue=inbox, on_message_callback=self._on_mining_task,
                                     auto_ack=False)
        self._channel.basic_consume(queue=results_q, on_message_callback=self._on_worker_result,
                                     auto_ack=True)
        self._channel.basic_consume(queue=registry_q, on_message_callback=self._on_worker_heartbeat,
                                     auto_ack=True)

        logger.info("Pool %s ready (fallback_workers=%d, heartbeat_timeout=%.0fs) — health on :%d",
                     self.pool_id, self._worker_count_fallback,
                     self._heartbeat_timeout, self.health_port)
        self._channel.start_consuming()

    def shutdown(self) -> None:
        self._shutdown.set()
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
            self._worker_heartbeats[data["worker_id"]] = data.get(
                "timestamp", time.time()
            )

    def _get_active_worker_count(self) -> int:
        """Return number of workers that sent a heartbeat recently."""
        cutoff = time.time() - self._heartbeat_timeout
        with self._heartbeat_lock:
            stale = [
                wid for wid, ts in self._worker_heartbeats.items() if ts < cutoff
            ]
            for wid in stale:
                del self._worker_heartbeats[wid]
            return len(self._worker_heartbeats)

    # ------------------------------------------------------------------
    # Mining task → partition & distribute
    # ------------------------------------------------------------------

    def _on_mining_task(self, ch: Any, method: Any, _props: Any, body: bytes) -> None:
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
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
            logger.info("No heartbeats yet; using fallback worker_count=%d", count)

        self._current_block_index = task.block_index
        self._current_fingerprint = task.fingerprint
        self._current_difficulty = task.difficulty
        self._current_task_id = task.task_id

        publish_tasks(
            self._channel,
            block_index=task.block_index,
            fingerprint=task.fingerprint,
            difficulty=task.difficulty,
            num_workers=count,
            range_size=task.range_max - task.range_min + 1,
            routing_key_prefix=f"pool.{self.pool_id}.task",
        )

        ch.basic_ack(delivery_tag=method.delivery_tag)

    # ------------------------------------------------------------------
    # Worker result → verify → forward to NCT
    # ------------------------------------------------------------------

    def _on_worker_result(self, _ch: Any, _method: Any, _props: Any, body: bytes) -> None:
        result = ResultMessage.from_json(body.decode())

        # Stale check
        if result.block_index != self._current_block_index:
            return

        # Verify PoW locally before forwarding
        pow_hash = hashlib.md5(
            (self._current_fingerprint + str(result.nonce)).encode()
        ).hexdigest()
        if pow_hash != result.hash:
            logger.warning("Pool %s: invalid PoW from %s — dropped", self.pool_id, result.worker_id)
            return
        if not pow_hash.startswith("0" * self._current_difficulty):
            logger.warning("Pool %s: difficulty not met by %s — dropped",
                           self.pool_id, result.worker_id)
            return

        # Forward valid solution to NCT
        self._channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=f"result.{self.pool_id}",
            body=result.to_json(),
        )
        logger.info("Pool %s: valid nonce %d from %s — forwarded to NCT",
                     self.pool_id, result.nonce, result.worker_id)

        # Abort pool workers
        self._broadcast_abort(self._current_task_id)

    # ------------------------------------------------------------------
    # Abort
    # ------------------------------------------------------------------

    def _broadcast_abort(self, task_id: str) -> None:
        msg = ControlMessage(action="abort", task_id=task_id)
        self._channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=f"pool.{self.pool_id}.control",
            body=msg.to_json(),
        )
        logger.info("Pool %s: broadcast abort for task %s", self.pool_id, task_id)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def _run_health(self) -> None:
        app = FastAPI(title=f"Pool {self.pool_id}", version="1.0.0")

        @app.get("/health", response_model=HealthResponse)
        def health() -> HealthResponse:
            return HealthResponse(status="ok")

        uvicorn.run(app, host="0.0.0.0", port=self.health_port, log_level="warning")


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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log_file = os.getenv("LOG_FILE")
    setup_logging(log_file)

    pool_id = os.getenv("POOL_ID", f"pool-{uuid.uuid4().hex[:6]}")
    rmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    worker_count = int(os.getenv("POOL_WORKER_COUNT", str(DEFAULT_WORKER_COUNT)))
    health_port = int(os.getenv("HEALTH_PORT", str(DEFAULT_HEALTH_PORT)))

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
