"""NCT — Node Coordinator for the distributed blockchain mining pool.

Orchestrates the full lifecycle of a block: transaction accumulation,
block creation, distributed mining via RabbitMQ, PoW verification,
and chain persistence into Redis.

Usage::

    python -m nct.nct
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI

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
    connect as redis_connect,
    get_balance,
    get_block,
    get_chain_height,
    get_latest_block,
    get_nonce,
    rebuild_state_from_chain,
    save_block,
    save_block_atomic,
    validate_chain,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def load_config() -> NCTConfig:
    difficulty = _env_int("DIFFICULTY", 4)
    if difficulty < 1 or difficulty > 10:
        raise ValueError(
            f"DIFFICULTY must be 1-10 (MD5 GPU mining infeasible beyond 10), "
            f"got {difficulty}"
        )

    return NCTConfig(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
        rabbitmq_url=os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
        worker_count=_env_int("WORKER_COUNT", 2),
        block_size=_env_int("BLOCK_SIZE", 5),
        block_timeout=_env_float("BLOCK_TIMEOUT", 30.0),
        difficulty=difficulty,
        nonce_space=_env_int("NONCE_SPACE", 1_000_000_000),
        port=_env_int("PORT", 8080),
        authority_pubkey=os.getenv("AUTHORITY_PUBKEY", ""),
    )


def verify_pow_result(
    fingerprint: str,
    difficulty: int,
    nonce: int,
    claimed_hash: str,
) -> tuple[bool, str]:
    """Check that *claimed_hash* is ``MD5(fingerprint + nonce)`` and meets
    the difficulty target.

    Returns ``(is_valid, actual_md5_hash)``.
    """
    pow_hash = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
    valid = (pow_hash == claimed_hash) and pow_hash.startswith("0" * difficulty)
    return valid, pow_hash


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
    if state.block_mined.is_set():
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
    valid, actual_hash = verify_pow_result(fingerprint, difficulty, result.nonce, result.hash)
    if not valid:
        logger.warning(
            "Invalid PoW from %s: claimed %s, actual %s (nonce=%d)",
            result.worker_id, result.hash, actual_hash, result.nonce,
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
    state.block_mined.set()

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
            logger.error("Chain is empty (no genesis block). Run init first.")
            time.sleep(2)
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

        while not mined and not state.shutdown.is_set():
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

            # Timeout — expand range and retry
            nonce_space *= 2
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

        # ---- Poll mining results ----
        method, _properties, body = channel.basic_get(
            queue=RESULTS_QUEUE, auto_ack=True,
        )
        if method and body:
            result = ResultMessage.from_json(body.decode())
            handle_result(state, redis_client, channel, result)
            had_work = True

        # ---- Poll worker registry (heartbeats) ----
        method, _properties, body = channel.basic_get(
            queue=WORKER_REGISTRY_QUEUE, auto_ack=True,
        )
        if method and body:
            data = json.loads(body.decode())
            if data.get("action") == "pool_no_workers":
                logger.warning(
                    "Pool '%s' reports no workers for block %s",
                    data.get("worker_id"), data.get("block_index"),
                )
            else:
                state.update_worker(data.get("worker_id", "unknown"))
            had_work = True

        if not had_work:
            time.sleep(0.1)

    logger.info("Result loop stopped")


# ---------------------------------------------------------------------------
# FastAPI health application
# ---------------------------------------------------------------------------


def create_health_app(state: NCTState, redis_client: Any) -> FastAPI:
    """Build a FastAPI app wired to the shared NCT state and Redis.

    The app is created once in ``main()`` and served by uvicorn in a
    background thread — same threading model as before, cleaner contracts.
    """
    app = FastAPI(title="NCT", version="1.0.0")

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
        )

    @app.post(
        "/transaction",
        response_model=TransactionResponse,
        status_code=201,
        responses={400: {"model": ErrorResponse}},
    )
    def create_transaction(tx: TransactionRequest) -> TransactionResponse:
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
        return TransactionResponse(tx_id=t.tx_id)

    @app.get("/balance/{address}", response_model=BalanceResponse)
    def get_balance_for_address(address: str) -> BalanceResponse:
        balance = get_balance(redis_client, address)
        return BalanceResponse(address=address, balance=balance)

    @app.get("/account/{pubkey}", response_model=AccountResponse)
    def get_account(pubkey: str) -> AccountResponse:
        """Return balance and next expected nonce for a public key."""
        balance = get_balance(redis_client, pubkey)
        nonce = get_nonce(redis_client, pubkey)
        return AccountResponse(address=pubkey, balance=balance, nonce=nonce)

    @app.get("/chain", response_model=list[dict])
    def get_chain() -> list[dict]:
        """Return the full serialised chain (audit trail)."""
        height = get_chain_height(redis_client)
        result: list[dict] = []
        for i in range(height):
            blk = get_block(redis_client, i)
            if blk is not None:
                result.append(blk.to_dict())
        return result

    return app


def health_loop(app: FastAPI, port: int) -> None:
    """Thread 3 — serve the FastAPI app via uvicorn."""
    logger.info("Health server listening on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


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
    channel = rmq_conn.channel()
    declare_topology(channel)

    # Mutable refs so both threads share reconnection state (audit H2)
    conn_ref: list[Any] = [rmq_conn]
    ch_ref: list[Any] = [channel]

    # ---- Shared state ----
    state = NCTState()
    state.chain_height = 1  # genesis is block 0 → height = 1

    # ---- FastAPI app (wired to shared state) ----
    health_app = create_health_app(state, redis_client)

    # ---- Threads ----
    threads = [
        threading.Thread(target=block_loop, args=(state, redis_client, conn_ref, ch_ref, config),
                         name="block-loop", daemon=True),
        threading.Thread(target=result_loop, args=(state, redis_client, conn_ref, ch_ref,
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
