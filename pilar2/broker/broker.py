"""RabbitMQ topology and operations for the blockchain mining pool.

All ``pika`` imports are lazy — the module is importable without a
RabbitMQ installation.  Only functions that actually connect to a broker
will trigger the import.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .messages import ControlMessage, ResultMessage, TaskMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXCHANGE = "blockchain"
RESULTS_QUEUE = "mining_results"
WORKER_REGISTRY_QUEUE = "worker_registry"
CONTROL_ROUTING_KEY = "control"

# ---------------------------------------------------------------------------
# Persistent message helper (audit H1)
# ---------------------------------------------------------------------------

_PERSISTENT_PROPS: Any = None


def persistent_props() -> Any:
    """Return ``pika.BasicProperties(delivery_mode=2)`` for message durability.

    Setting ``delivery_mode=2`` makes messages survive RabbitMQ restarts
    when published to durable queues.  Without it, messages are transient
    and are lost if the broker restarts, even with durable queues.

    The ``pika`` import and the instance are cached at module level so
    repeated calls do not re-import or re-allocate.
    """
    global _PERSISTENT_PROPS
    if _PERSISTENT_PROPS is None:
        import pika  # type: ignore[import-untyped]
        _PERSISTENT_PROPS = pika.BasicProperties(delivery_mode=2)
    return _PERSISTENT_PROPS


# Re-exported for convenience (pool, worker)
__all__: list[str] = []


# ---------------------------------------------------------------------------
# Reconnection helpers (audit H2)
# ---------------------------------------------------------------------------

_RECONNECT_MAX_RETRIES = int(os.getenv("RABBITMQ_RECONNECT_MAX_RETRIES", "20"))
_RECONNECT_BASE_DELAY = float(os.getenv("RABBITMQ_RECONNECT_BASE_DELAY", "1.0"))
_RECONNECT_MAX_DELAY = float(os.getenv("RABBITMQ_RECONNECT_MAX_DELAY", "30.0"))


def is_recoverable_rabbitmq_error(exc: BaseException) -> bool:
    """Return ``True`` for transient RabbitMQ errors worth retrying."""
    name = type(exc).__name__
    return any(term in name for term in (
        "ConnectionClosed", "StreamLostError", "ChannelClosed",
        "AMQPConnectionError", "ConnectionError", "ChannelError",
        "TimeoutError",
    ))


def reconnect_rabbitmq(
    rmq_url: str,
    max_retries: int = _RECONNECT_MAX_RETRIES,
    base_delay: float = _RECONNECT_BASE_DELAY,
    max_delay: float = _RECONNECT_MAX_DELAY,
) -> tuple[Any, Any]:
    """Return a fresh ``(connection, channel)`` pair with topology declared.

    Retries with exponential backoff on transient errors.
    """
    for attempt in range(max_retries):
        try:
            conn = get_connection(url=rmq_url)
            ch = conn.channel()
            declare_topology(ch)
            if attempt > 0:
                logger.info("Reconnected to RabbitMQ (attempt %d/%d)", attempt + 1, max_retries)
            return conn, ch
        except Exception as exc:
            if not is_recoverable_rabbitmq_error(exc):
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(
                "RabbitMQ reconnect attempt %d/%d failed: %s. Retrying in %.1fs…",
                attempt + 1, max_retries, exc, delay,
            )
            time.sleep(delay)

    raise ConnectionError(
        f"Failed to reconnect to RabbitMQ at {rmq_url} after {max_retries} attempts"
    )


def _on_return(
    _channel: Any, method: Any, _properties: Any, body: bytes,
) -> None:
    """Log messages returned by RabbitMQ when no queue is bound (audit L1)."""
    logger.warning(
        "Message returned: routing_key=%s reply_code=%s reply_text=%s body=%.200s",
        method.routing_key, method.reply_code, method.reply_text, body,
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection(
    url: Optional[str] = None,
    max_retries: int = 10,
    retry_delay: float = 2.0,
) -> Any:
    """Return a connected ``pika.BlockingConnection``, retrying on failure.

    Parameters
    ----------
    url:
        RabbitMQ connection URL.  Defaults to ``RABBITMQ_URL`` env var
        or ``amqp://localhost:5672``.
    """
    import pika  # type: ignore[import-untyped]
    import pika.exceptions

    if url is None:
        url = os.getenv("RABBITMQ_URL", "amqp://localhost:5672")

    # AMQP heartbeat for fast dead-connection detection (default 5 s).
    # pika.URLParameters parses ?heartbeat=N from the query string.
    amqp_heartbeat = int(os.getenv("AMQP_HEARTBEAT", "5"))
    if "heartbeat=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}heartbeat={amqp_heartbeat}"

    for attempt in range(1, max_retries + 1):
        try:
            connection = pika.BlockingConnection(pika.URLParameters(url))
            logger.info("Connected to RabbitMQ at %s", url)
            return connection
        except pika.exceptions.AMQPConnectionError:
            if attempt == max_retries:
                raise
            logger.warning(
                "RabbitMQ not ready (attempt %d/%d), retrying in %ds…",
                attempt, max_retries, retry_delay,
            )
            time.sleep(retry_delay)
    raise ConnectionError(f"Cannot reach RabbitMQ at {url}")  # unreachable


# ---------------------------------------------------------------------------
# Topology  (idempotent — safe to call on every startup)
# ---------------------------------------------------------------------------


def declare_topology(channel: Any) -> None:
    """Create the exchange, queues, and bindings.

    Call this once per connection before publishing or consuming.
    """
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)

    # Shared results queue (workers/pools → NCT)
    channel.queue_declare(queue=RESULTS_QUEUE, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=RESULTS_QUEUE, routing_key="result.*")

    # Worker registry queue (workers → NCT heartbeats & registration)
    channel.queue_declare(queue=WORKER_REGISTRY_QUEUE, durable=True)
    # Audit H2 corrected: use worker.# so pools (worker.pool-a) and their
    # status messages (worker.pool-a.status) match, not just solo workers
    # (worker.heartbeat).  The pool is the worker from the NCT's perspective.
    channel.queue_bind(exchange=EXCHANGE, queue=WORKER_REGISTRY_QUEUE, routing_key="worker.#")

    # Audit L1: log returned messages (mandatory=True publishes with no binding)
    channel.add_on_return_callback(_on_return)


# ---------------------------------------------------------------------------
# Coordinator (NCT) helpers
# ---------------------------------------------------------------------------


def publish_tasks(
    channel: Any,
    block_index: int,
    fingerprint: str,
    difficulty: int,
    routing_key_prefix: str,
    num_workers: int = 3,
    range_size: int = 1_000_000_000,
) -> list[TaskMessage]:
    """Partition the nonce space and publish one task per partition.

    Used by pool coordinators to distribute sub-ranges to their workers.
    Returns the list of published messages.
    """
    chunk = range_size // num_workers
    tasks: list[TaskMessage] = []

    for i in range(num_workers):
        r_min = i * chunk
        r_max = range_size - 1 if i == num_workers - 1 else (i + 1) * chunk - 1

        task = TaskMessage.create(
            block_index=block_index,
            fingerprint=fingerprint,
            difficulty=difficulty,
            range_min=r_min,
            range_max=r_max,
        )
        tasks.append(task)

        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=f"{routing_key_prefix}.{i}",
            body=task.to_json(),
            properties=persistent_props(),
            mandatory=True,
        )

    logger.info(
        "Published %d sub-tasks for block %d (difficulty=%d, range=[0, %d])",
        num_workers, block_index, difficulty, range_size,
    )
    return tasks


def publish_mining_task(
    channel: Any,
    block_index: int,
    fingerprint: str,
    difficulty: int,
    range_size: int = 1_000_000_000,
) -> TaskMessage:
    """Publish a single mining task to all consumers (fanout via topic).

    Pools and solo miners bind their own queues to ``task.mining``.
    One message → every subscriber gets a copy → they compete.
    """
    task = TaskMessage.create(
        block_index=block_index,
        fingerprint=fingerprint,
        difficulty=difficulty,
        range_min=0,
        range_max=range_size - 1,
    )
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key="task.mining",
        body=task.to_json(),
        properties=persistent_props(),
        mandatory=True,
    )
    logger.info("Published mining task for block %d (range=[0, %d])", block_index, range_size)
    return task


def declare_consumer_queue(channel: Any, queue_name: str, routing_key: str) -> None:
    """Declare a durable queue and bind it to a routing key.

    Called by pools and solo miners when they start up, so each consumer
    gets its own copy of broadcast messages.
    """
    channel.queue_declare(queue=queue_name, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=queue_name, routing_key=routing_key)
    logger.info("Declared consumer queue %s (bind: %s)", queue_name, routing_key)


def broadcast_abort(channel: Any, task_id: str) -> None:
    """Publish an abort signal so all workers stop searching for *task_id*."""
    msg = ControlMessage(action="abort", task_id=task_id)
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=CONTROL_ROUTING_KEY,
        body=msg.to_json(),
        properties=persistent_props(),
        mandatory=True,
    )
    logger.info("Broadcast abort for task %s", task_id)