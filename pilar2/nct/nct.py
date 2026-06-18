"""NCT — Node Coordinator for the distributed blockchain mining pool.

Orchestrates the full lifecycle of a block: transaction accumulation,
block creation, distributed mining via RabbitMQ, PoW verification,
and chain persistence into Redis.

Usage::

    python -m nct.nct
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from broker.broker import (
    RESULTS_QUEUE,
    WORKER_REGISTRY_QUEUE,
    broadcast_abort,
    declare_topology,
    get_connection,
    is_recoverable_rabbitmq_error,
    publish_mining_task,
    reconnect_rabbitmq,
)
from broker.messages import ResultMessage
from nct.state import NCTConfig, NCTState
from shared.block import Block, Transaction
from shared.schemas import (
    AccountResponse,
    BalanceResponse,
    ErrorResponse,
    HealthResponse,
    NCTStatusResponse,
    TransactionRequest,
    TransactionResponse,
)
from storage.chain_store import (
    add_discarded_tx,
    connect as redis_connect,
    get_balance,
    get_block,
    get_chain_height,
    get_discarded_txns,
    get_latest_block,
    get_nonce,
    rebuild_state_from_chain,
    restore_pending_txs,
    save_block,
    save_block_atomic,
    save_pending_tx,
    trim_pending_txs,
    validate_chain,
)

from shared.env import env_int, env_float

# ---------------------------------------------------------------------------
# Rate limiting (H4 — audit-05)
# ---------------------------------------------------------------------------
_limiter = Limiter(key_func=get_remote_address)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_config() -> NCTConfig:
    difficulty = env_int("DIFFICULTY", 4, min_val=1)
    if difficulty > 10:
        raise SystemExit(
            f"DIFFICULTY must be <= 10 (MD5 GPU mining infeasible beyond 10), "
            f"got {difficulty}"
        )

    authority_pubkey = os.getenv("AUTHORITY_PUBKEY", "")

    if not authority_pubkey:
        logger.warning("AUTHORITY_PUBKEY is not configured — EARN transactions will be rejected")

    return NCTConfig(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
        rabbitmq_url=os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
        worker_count=env_int("WORKER_COUNT", 2),
        block_size=env_int("BLOCK_SIZE", 5),
        block_timeout=env_float("BLOCK_TIMEOUT", 30.0),
        difficulty=difficulty,
        nonce_space=env_int("NONCE_SPACE", 1_000_000_000),
        port=env_int("PORT", 8080),
        rate_limit=os.getenv("RATE_LIMIT", "100/minute"),
        authority_pubkey=authority_pubkey,
    )


def drain_pool_validated(
    state: NCTState,
    redis_client: Any,
    max_count: int,
) -> list[Transaction]:
    """Drain the transaction pool applying balance validation for SPEND txns.

    Maintains an in-memory *overlay* that tracks per-student deltas
    accumulated during this block's assembly.  This prevents double-spend
    within a single block without requiring synchronous balance checks at
    POST time.

    EARN transactions are always accepted (structural validation already
    passed).  SPEND transactions are accepted only when::

        confirmed_balance + overlay_delta >= amount

    Discarded transactions are **not** returned to the pool — the client
    must re-send the POST if it wants to retry.
    """
    candidates = state.drain_pool(max_count)
    overlay: dict[str, int] = {}   # student_id → accumulated delta in this block
    nonce_overlay: dict[str, int] = {}  # sender_pubkey → next expected nonce in this block
    valid: list[Transaction] = []
    discarded: list[Transaction] = []

    for tx in candidates:
        # Nonce validation — reject if nonce was already consumed
        # (e.g. another transaction from same sender mined between POST and now,
        #  or another tx from the same sender already accepted in this block)
        current_nonce = get_nonce(redis_client, tx.sender_pubkey)
        expected = nonce_overlay.get(tx.sender_pubkey, current_nonce)
        if tx.nonce != expected:
            discarded.append(tx)
            logger.warning(
                "TX descartada — nonce inválido: sender=%s esperado=%d recibido=%d",
                tx.sender_pubkey, expected, tx.nonce,
            )
            continue

        # Advance nonce for this sender within the block
        nonce_overlay[tx.sender_pubkey] = tx.nonce + 1

        if tx.tx_type == "EARN":
            valid.append(tx)
            overlay[tx.receiver_pubkey] = overlay.get(tx.receiver_pubkey, 0) + tx.amount

        elif tx.tx_type == "SPEND":
            confirmed = get_balance(redis_client, tx.sender_pubkey)
            in_flight = overlay.get(tx.sender_pubkey, 0)
            effective = confirmed + in_flight

            if effective >= tx.amount:
                valid.append(tx)
                overlay[tx.sender_pubkey] = in_flight - tx.amount
            else:
                discarded.append(tx)
                logger.warning(
                    "SPEND descartado — saldo insuficiente: sender=%s "
                    "confirmado=%d en_vuelo=%d requerido=%d concept=%s",
                    tx.sender_pubkey, confirmed, in_flight, tx.amount, tx.concept,
                )

    if discarded:
        logger.info("%d transacción(es) descartada(s) por saldo insuficiente", len(discarded))
        # Audit M2: record discarded tx_ids so clients can discover them
        for tx in discarded:
            add_discarded_tx(redis_client, tx.sender_pubkey, tx.tx_id)

    # Audit L2: remove drained transactions from Redis so they are not
    # re-queued on restart.  The count is (valid + discarded) — everything
    # that was taken out of the pool.
    drained_count = len(valid) + len(discarded)
    if drained_count > 0:
        trim_pending_txs(redis_client, drained_count)

    return valid


def handle_result(
    state: NCTState,
    redis_client: Any,
    channel: Any,
    result: ResultMessage,
) -> bool:
    """Process a mining result: verify PoW, complete and persist the block,
    broadcast abort, and signal the block loop.

    Returns ``True`` if the block was successfully mined and persisted.
    """
    # ---- Duplicate guard: block already mined by another worker ----
    # Audit M2: use mining_active() instead of block_mined.is_set() to
    # decouple the "is mining in progress" semantic from the Event used
    # to signal the block_loop thread.
    if not state.mining_active():
        logger.debug("Resultado duplicado para bloque ya minado — descartado")
        return False

    current_block, fingerprint, difficulty = state.get_current_for_verification()
    if current_block is None:
        logger.debug("No current mining job — ignoring result for block %d", result.block_index)
        return False

    # ---- Stale check ----
    if result.block_index != current_block.index:
        logger.debug("Stale result for block %d (current is %d), ignoring",
                      result.block_index, current_block.index)
        return False

    # ---- PoW verification ----
    valid, actual_hash = Block.verify_result(fingerprint, difficulty, result.nonce, result.hash)
    if not valid:
        current_block_index = current_block.index if current_block else "?"
        logger.warning(
            "Invalid PoW from %s for block %s: claimed %s, actual %s "
            "(nonce=%d, difficulty=%d)",
            result.worker_id, current_block_index,
            result.hash, actual_hash, result.nonce,
            difficulty,
        )
        return False

    # ---- Complete the block ----
    current_block.nonce = result.nonce
    current_block.hash = current_block.compute_hash()

    # ---- Persist atomically (audit H3, M4) ----
    save_block_atomic(redis_client, current_block)
    state.chain_height = current_block.index + 1

    # ---- Broadcast abort / signal block loop ----
    broadcast_abort(channel, result.task_id)
    state.mark_mining_complete()

    logger.info(
        "Block %d mined by %s (nonce=%d, hash=%s)",
        current_block.index, result.worker_id, result.nonce, current_block.hash,
    )
    return True


def accumulate_transactions(
    state: NCTState,
    redis_client: Any,
    config: NCTConfig,
) -> list[Transaction]:
    """Block until the transaction pool meets the threshold or a timeout is reached.

    At least one transaction is required; returns an empty list only on shutdown.
    Transactions are validated for sufficient balance via ``drain_pool_validated``.
    """
    # Wait for at least one transaction
    while state.pool_size() == 0 and not state.shutdown.is_set():
        time.sleep(0.5)

    if state.shutdown.is_set():
        return []

    # Wait until BLOCK_SIZE is reached or BLOCK_TIMEOUT expires
    deadline = time.time() + config.block_timeout
    while state.pool_size() < config.block_size and time.time() < deadline:
        time.sleep(0.5)

    return drain_pool_validated(state, redis_client, config.block_size)


# ---------------------------------------------------------------------------
# Reconnection (audit H2)
# ---------------------------------------------------------------------------


def _ensure_rabbitmq_alive(
    conn_ref: list[Any],
    ch_ref: list[Any],
    rmq_url: str,
) -> None:
    """Check that the current RabbitMQ channel is alive; reconnect if dead.

    *conn_ref* and *ch_ref* are mutable single-element lists so that
    multiple threads can share the reconnection.
    """
    channel = ch_ref[0]
    connection = conn_ref[0]

    # Quick health check — passive exchange declare is the lightest way
    # to know if the channel is still usable.
    try:
        if channel is None or connection is None:
            raise ConnectionError("No channel")
        if not connection.is_open:
            raise ConnectionError("Connection closed")
        if not channel.is_open:
            raise ConnectionError("Channel closed")
        channel.exchange_declare(exchange="blockchain", passive=True)
    except Exception as exc:
        if not is_recoverable_rabbitmq_error(exc):
            raise
        logger.warning("RabbitMQ connection lost — reconnecting…")
        new_conn, new_ch = reconnect_rabbitmq(rmq_url)
        conn_ref[0] = new_conn
        ch_ref[0] = new_ch


# ---------------------------------------------------------------------------
# Loops (one per thread)
# ---------------------------------------------------------------------------


def block_loop(
    state: NCTState,
    redis_client: Any,
    conn_ref: list[Any],
    ch_ref: list[Any],
    config: NCTConfig,
) -> None:
    """Thread 1 — accumulate transactions, create blocks, publish mining tasks."""
    logger.info("Block loop started")

    while not state.shutdown.is_set():
        # Ensure RabbitMQ is alive before any publish (audit H2)
        _ensure_rabbitmq_alive(conn_ref, ch_ref, config.rabbitmq_url)
        channel = ch_ref[0]

        # 1. Accumulate transactions
        txs = accumulate_transactions(state, redis_client, config)
        if not txs:
            continue  # shutdown or empty — retry

        # 2. Get latest block for chaining
        latest = get_latest_block(redis_client)
        if latest is None:
            # Audit L3: Redis may have been wiped mid-run — recreate genesis
            logger.warning(
                "Chain is empty (no genesis block). Recreating genesis…"
            )
            ensure_genesis(redis_client)
            continue

        # 3. Create new block
        block = Block(
            index=latest.index + 1,
            timestamp=time.time(),
            transactions=txs,
            previous_hash=latest.hash,
            difficulty=config.difficulty,
        )
        logger.info("Created block %d with %d transactions", block.index, len(txs))

        # 4. Mining loop with range expansion on timeout
        nonce_space = config.nonce_space
        mined = False
        _consecutive_dead_checks = 0

        while not mined and not state.shutdown.is_set():
            # ---- Pool liveness gate (audit H2) ----
            if state.all_pools_dead():
                _consecutive_dead_checks += 1
                if _consecutive_dead_checks == 1:
                    logger.critical(
                        "All pools dead — no workers available for block %d. "
                        "Waiting for pool recovery…",
                        block.index,
                    )
                # Backoff: wait, then re-check.  Don't publish to dead pools.
                backoff = min(_consecutive_dead_checks * 5, 60)
                if state.block_mined.wait(timeout=backoff):
                    mined = True
                    break
                continue
            _consecutive_dead_checks = 0

            state.set_current_block(block, nonce_space)

            publish_mining_task(
                channel,
                block_index=block.index,
                fingerprint=block.fingerprint,
                difficulty=config.difficulty,
                range_size=nonce_space,
            )

            logger.info("Waiting for PoW solution for block %d (nonce_space=%d)...",
                         block.index, nonce_space)

            # Wait for the result loop to signal completion
            mined = state.block_mined.wait(timeout=config.block_timeout)

            if mined:
                break

            # Timeout — expand range and retry (audit L1: capped)
            nonce_space = min(nonce_space * 2, config.max_nonce_space)
            if nonce_space >= config.max_nonce_space:
                logger.critical(
                    "MAX_NONCE_SPACE (%d) reached for block %d — "
                    "mining may be broken (no workers, difficulty too high, "
                    "or bug in miner)",
                    config.max_nonce_space, block.index,
                )
            logger.warning("Mining timeout for block %d, expanding to %d", block.index, nonce_space)

    logger.info("Block loop stopped")


def result_loop(
    state: NCTState,
    redis_client: Any,
    conn_ref: list[Any],
    ch_ref: list[Any],
    rmq_url: str,
) -> None:
    """Thread 2 — poll mining results and worker registry, verify PoW, persist blocks."""
    logger.info("Result loop started")

    while not state.shutdown.is_set():
        # Ensure RabbitMQ is alive before any poll (audit H2)
        _ensure_rabbitmq_alive(conn_ref, ch_ref, rmq_url)
        channel = ch_ref[0]

        had_work = False

        # ---- Poll mining results (audit H1: manual ack for crash safety) ----
        method, _properties, body = channel.basic_get(
            queue=RESULTS_QUEUE, auto_ack=False,
        )
        if method and body:
            try:
                result = ResultMessage.from_json(body.decode())
                handle_result(state, redis_client, channel, result)
            except Exception:
                logger.exception(
                    "Failed to process mining result — nacking (no requeue)"
                )
                try:
                    channel.basic_nack(
                        delivery_tag=method.delivery_tag, requeue=False,
                    )
                except Exception:
                    pass  # channel may be dead; reconnection handles it
            else:
                # Only ack on success — protects against crash between
                # basic_get and handle_result (audit H1)
                try:
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                except Exception:
                    pass  # channel may be dead; reconnection handles it
            had_work = True

        # ---- Poll worker registry (heartbeats + pool liveness) ----
        method, _properties, body = channel.basic_get(
            queue=WORKER_REGISTRY_QUEUE, auto_ack=False,
        )
        if method and body:
            try:
                data = json.loads(body.decode())
                if data.get("action") == "pool_no_workers":
                    pool_id = data.get("worker_id", "unknown")
                    logger.error(
                        "Pool '%s' reports no workers for block %s",
                        pool_id, data.get("block_index"),
                    )
                    state.mark_pool_dead(pool_id)
                elif data.get("role") == "pool":
                    # Pool heartbeat (pool registers as a "worker" from NCT's view)
                    state.mark_pool_alive(data.get("worker_id", "unknown"))
                else:
                    state.update_worker(data.get("worker_id", "unknown"))
            except Exception:
                logger.exception(
                    "Failed to process worker registry message — nacking"
                )
                try:
                    channel.basic_nack(
                        delivery_tag=method.delivery_tag, requeue=False,
                    )
                except Exception:
                    pass
            else:
                try:
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                except Exception:
                    pass
            had_work = True

        # Audit L2: exponential backoff — ramps from 0.1s up to 1.0s
        # when idle, resets to 0.1s the instant any work is found.
        if had_work:
            idle_ms = 100
        else:
            time.sleep(idle_ms / 1000.0)
            idle_ms = min(idle_ms * 2, 1000)

    logger.info("Result loop stopped")


# ---------------------------------------------------------------------------
# FastAPI health application
# ---------------------------------------------------------------------------


def create_health_app(state: NCTState, redis_client: Any, config: NCTConfig) -> FastAPI:
    """Build a FastAPI app wired to the shared NCT state and Redis.

    The app is created once in ``main()`` and served by uvicorn in a
    background thread — same threading model as before, cleaner contracts.

    *config* is passed explicitly (audit L1: no closure capture) so the
    function can be tested in isolation without depending on the ``main()``
    scope.
    """
    app = FastAPI(title="NCT", version="1.0.0")

    # Rate-limit middleware (H4 — audit-05)
    app.state.limiter = _limiter

    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        from fastapi.responses import JSONResponse as JR
        return JR(status_code=429, content={"error": "Too many requests — rate limit exceeded"})

    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/status", response_model=NCTStatusResponse)
    def status() -> NCTStatusResponse:
        cb, _, _ = state.get_current_for_verification()
        return NCTStatusResponse(
            chain_height=state.chain_height,
            pending_transactions=state.pool_size(),
            current_block=cb.index if cb else None,
            active_pools=state.active_pools(),
        )

    @app.post(
        "/transaction",
        response_model=TransactionResponse,
        status_code=201,
        responses={400: {"model": ErrorResponse}, 429: {"description": "Rate limit exceeded"}},
    )
    @_limiter.limit(config.rate_limit)
    def create_transaction(request: Request, tx: TransactionRequest) -> TransactionResponse:
        from fastapi.responses import JSONResponse
        from shared.crypto import verify as crypto_verify

        t = Transaction(
            sender_pubkey=tx.sender_pubkey,
            receiver_pubkey=tx.receiver_pubkey,
            amount=tx.amount,
            tx_type=tx.tx_type,
            concept=tx.concept,
            signature=tx.signature,
            nonce=tx.nonce,
        )

        # 1. Structural validation (pubkey lengths, amount, concept, etc.)
        errors = t.validate()
        if errors:
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(error="; ".join(errors)).model_dump(),
            )

        # 2. Signature verification (Ed25519 over tx_id)
        if not crypto_verify(t.sender_pubkey, t.tx_id.encode(), t.signature):
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(
                    error="invalid signature — does not match sender_pubkey"
                ).model_dump(),
            )

        # 3. Nonce validation — prevents replay attacks
        current_nonce = get_nonce(redis_client, t.sender_pubkey)
        if t.nonce != current_nonce:
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(
                    error=f"invalid nonce: expected {current_nonce}, got {t.nonce}"
                ).model_dump(),
            )

        # 4. Authority check — only the configured authority can issue EARN
        if t.tx_type == "EARN":
            if not config.authority_pubkey:
                return JSONResponse(
                    status_code=400,
                    content=ErrorResponse(
                        error="EARN transactions require AUTHORITY_PUBKEY to be configured"
                    ).model_dump(),
                )
            if t.sender_pubkey != config.authority_pubkey:
                return JSONResponse(
                    status_code=400,
                    content=ErrorResponse(
                        error="EARN sender_pubkey does not match AUTHORITY_PUBKEY"
                    ).model_dump(),
                )

        state.add_transaction(t)
        # Audit L2: persist so the tx survives an NCT restart
        save_pending_tx(redis_client, t)
        return TransactionResponse(tx_id=t.tx_id)

    @app.get("/balance/{address}", response_model=BalanceResponse)
    def get_balance_for_address(address: str) -> BalanceResponse:
        balance = get_balance(redis_client, address)
        return BalanceResponse(address=address, balance=balance)

    @app.get("/account/{pubkey}", response_model=AccountResponse)
    def get_account(pubkey: str) -> AccountResponse:
        """Return balance, nonce, and discarded transaction ids (audit M2)."""
        balance = get_balance(redis_client, pubkey)
        nonce = get_nonce(redis_client, pubkey)
        discarded = get_discarded_txns(redis_client, pubkey)
        return AccountResponse(
            address=pubkey,
            balance=balance,
            nonce=nonce,
            discarded_transactions=discarded,
        )

    @app.get("/chain", response_model=list[dict])
    def get_chain(
        start: int = 0,
        count: int = 20,
    ) -> list[dict]:
        """Return a slice of the serialised chain (audit trail).

        Query params:
            start (int): first block index to return (0-based, default 0)
            count (int): max blocks to return (default 20, max 100)
        """
        count = min(max(count, 0), 100)
        if count == 0:
            return []

        height = get_chain_height(redis_client)
        start = max(start, 0)

        result: list[dict] = []
        end = min(start + count, height)
        for i in range(start, end):
            blk = get_block(redis_client, i)
            if blk is not None:
                result.append(blk.to_dict())
        return result

    return app


def health_loop(app: FastAPI, port: int) -> None:
    """Thread 3 — serve the FastAPI app via uvicorn."""
    logger.info("Health server listening on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", limit_concurrency=100)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def ensure_genesis(redis_client: Any) -> None:
    """Create and persist the genesis block if the chain is empty.

    If the chain already exists, always rebuild balances and nonces from
    scratch (idempotent, cheap for PoC chain sizes).  Then run a full
    structural validation of the chain.
    """
    existing = get_latest_block(redis_client)
    if existing is None:
        genesis = Block.create_genesis()
        save_block(redis_client, genesis)
        logger.info("Genesis block created (hash=%s)", genesis.hash)
        return

    # Always rebuild derived state on startup (audit L3).
    # Idempotent, O(n) in chain height — acceptable for a PoC.
    chain_height = get_chain_height(redis_client)
    logger.info("Rebuilding state from %d block(s)…", chain_height)
    rebuild_state_from_chain(redis_client)

    # Validate structural integrity on every startup (audit M3)
    errors = validate_chain(redis_client)
    if errors:
        logger.error("Chain validation failed: %s", errors)
    else:
        logger.info("Chain validation passed (%d blocks)", chain_height)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log_file = os.getenv("LOG_FILE")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=handlers,
    )

    config = load_config()
    logger.info("NCT starting with config: %s", config)

    # ---- Redis ----
    redis_client = redis_connect()
    ensure_genesis(redis_client)

    # ---- RabbitMQ ----
    rmq_conn = get_connection(url=config.rabbitmq_url)
    # Audit C1: BlockingChannel is NOT thread-safe.  Create one channel per
    # thread — block_loop publishes mining tasks, result_loop consumes
    # results and polls the worker registry.  Sharing a single channel
    # across threads causes AMQP frame corruption under load.
    block_channel = rmq_conn.channel()
    result_channel = rmq_conn.channel()
    declare_topology(block_channel)  # queues/exchanges — idempotent

    # Each thread gets its own (conn_ref, ch_ref) pair so reconnection
    # is independent — one thread reconnecting does not invalidate the
    # other's channel.
    block_conn_ref: list[Any] = [rmq_conn]
    block_ch_ref: list[Any] = [block_channel]
    result_conn_ref: list[Any] = [rmq_conn]
    result_ch_ref: list[Any] = [result_channel]

    # ---- Shared state ----
    state = NCTState(pool_timeout=config.pool_timeout)
    state.chain_height = 1  # genesis is block 0 → height = 1

    # Audit L2: restore pending transactions from Redis so they survive
    # an NCT crash/restart.  Transactions already mined (but not yet trimmed
    # from Redis) will be caught by nonce validation in drain_pool_validated.
    pending = restore_pending_txs(redis_client)
    if pending:
        for tx in pending:
            state.add_transaction(tx)
        logger.info("Restored %d pending transaction(s) from Redis", len(pending))

    # ---- FastAPI app (wired to shared state) ----
    health_app = create_health_app(state, redis_client, config)

    # ---- Threads ----
    threads = [
        threading.Thread(target=block_loop,
                         args=(state, redis_client, block_conn_ref, block_ch_ref, config),
                         name="block-loop", daemon=True),
        threading.Thread(target=result_loop,
                         args=(state, redis_client, result_conn_ref, result_ch_ref,
                               config.rabbitmq_url),
                         name="result-loop", daemon=True),
        threading.Thread(target=health_loop, args=(health_app, config.port),
                         name="health-loop", daemon=True),
    ]

    for t in threads:
        t.start()

    # ---- Graceful shutdown ----
    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        state.shutdown.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep main thread alive until shutdown
    try:
        while not state.shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        state.shutdown.set()

    logger.info("NCT stopped")


if __name__ == "__main__":
    main()
