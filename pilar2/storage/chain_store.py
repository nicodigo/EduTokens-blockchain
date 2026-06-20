"""Redis-backed blockchain persistence layer.

Stores blocks as JSON strings in a Redis List.  Each block is appended
with ``RPUSH``, giving an append-only structure that mirrors the
conceptual blockchain.

Usage::

    from storage.chain_store import (
        connect, save_block, get_block, get_latest_block,
        get_chain_height, validate_chain,
    )

    redis_client = connect()
    save_block(redis_client, genesis)
    print(get_chain_height(redis_client))   # → 1
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from redis import Redis

from shared.block import Block, Transaction

# ---------------------------------------------------------------------------
# Redis key layout
# ---------------------------------------------------------------------------

BLOCKS_KEY = "blockchain:blocks"
BALANCE_PREFIX = "balance:"
NONCE_PREFIX = "nonce:"
DISCARDED_PREFIX = "discarded:"
PENDING_TXS_KEY = "blockchain:pending_txs"  # audit L2: crash recovery

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect() -> "Redis":
    """Return a Redis client configured from the ``REDIS_URL`` environment variable.

    Defaults to ``redis://localhost:6379`` when the variable is not set.

    ``redis-py`` is imported lazily so the rest of the module is usable
    without a Redis installation (e.g. during testing).
    """
    # fmt: off
    from redis import Redis          # type: ignore[import-untyped]
    from redis.exceptions import RedisError
    # fmt: on

    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    client: Redis = Redis.from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]
    try:
        client.ping()
    except RedisError as exc:
        raise ConnectionError(f"Could not connect to Redis at {url}") from exc
    return client


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def save_block(client: Any, block: Block) -> None:
    """Append *block* to the end of the chain (non-atomic).

    Prefer :func:`save_block_atomic` for production use — it persists the
    block, balances, and nonces in a single Redis transaction.

    The caller is responsible for ensuring the block is valid and
    correctly chained to the previous block.
    """
    payload = json.dumps(block.to_dict(), sort_keys=True)
    client.rpush(BLOCKS_KEY, payload)


def save_block_atomic(client: Any, block: Block) -> None:
    """Append *block* and update balances/nonces in one Redis transaction.

    Uses a ``MULTI/EXEC`` pipeline so the block, all balance changes, and
    all nonce updates are persisted atomically.  A crash between individual
    operations cannot leave the chain with a stored block but stale indexes
    (audit H3, M4).

    This is the recommended production path for ``handle_result``.
    :func:`save_block` is still available for genesis creation and other
    single-block operations that don't require balance updates.
    """
    payload = json.dumps(block.to_dict(), sort_keys=True)
    pipe = client.pipeline(transaction=True)

    # 1. Append the block JSON to the chain list
    pipe.rpush(BLOCKS_KEY, payload)

    # 2. Update balances for every transaction in the block
    for tx in block.transactions:
        if tx.tx_type == "EARN":
            pipe.incrby(f"{BALANCE_PREFIX}{tx.receiver_pubkey}", tx.amount)
        elif tx.tx_type == "SPEND":
            pipe.incrby(f"{BALANCE_PREFIX}{tx.sender_pubkey}", -tx.amount)

    # 3. Advance nonces for every sender
    for tx in block.transactions:
        pipe.set(f"{NONCE_PREFIX}{tx.sender_pubkey}", tx.nonce + 1)

    pipe.execute()

    import logging
    logger = logging.getLogger(__name__)
    logger.debug(
        "Block %d persisted atomically with %d balance/nonce updates",
        block.index,
        len(block.transactions) * 2,  # one balance + one nonce per tx
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_block(client: Any, index: int) -> Optional[Block]:
    """Return the block at *index*, or ``None`` if it does not exist.

    Indices are 0-based (``0`` is the genesis block).
    """
    raw = client.lindex(BLOCKS_KEY, index)
    if raw is None:
        return None
    return Block.from_dict(json.loads(raw))


def get_latest_block(client: Any) -> Optional[Block]:
    """Return the most recently appended block, or ``None`` if the chain is empty."""
    height = get_chain_height(client)
    if height == 0:
        return None
    return get_block(client, height - 1)


def get_chain_height(client: Any) -> int:
    """Return the number of blocks currently stored in the chain."""
    return client.llen(BLOCKS_KEY)


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def validate_chain(client: Any) -> list[dict]:
    """Walk the entire chain and return a list of validation errors.

    Each entry is ``{"index": i, "errors": [...]}``.  An empty list
    means the chain is structurally valid.
    """
    errors: list[dict] = []
    height = get_chain_height(client)

    for i in range(height):
        block = get_block(client, i)
        prev = get_block(client, i - 1) if i > 0 else None
        block_errors = block.validate(prev) if block else ["block is unreadable"]
        if block_errors:
            errors.append({"index": i, "errors": block_errors})

    return errors


# ---------------------------------------------------------------------------
# Balance index (derived cache over the blockchain)
# ---------------------------------------------------------------------------


def get_balance(client: Any, address: str) -> int:
    """Return the confirmed balance for *address*, or ``0`` if no entry exists.

    *address* is the public key (64 hex chars) of the account holder.
    """
    val = client.get(f"{BALANCE_PREFIX}{address}")
    return int(val) if val is not None else 0


# ---------------------------------------------------------------------------
# Nonce index (per-account sequential counter for replay protection)
# ---------------------------------------------------------------------------


def get_nonce(client: Any, pubkey: str) -> int:
    """Return the next expected nonce for *pubkey*, or ``0`` if no entry exists."""
    val = client.get(f"{NONCE_PREFIX}{pubkey}")
    return int(val) if val is not None else 0


def set_nonce(client: Any, pubkey: str, nonce: int) -> None:
    """Set the nonce for *pubkey* directly.

    Used during state rebuild.  In normal operation nonces are updated
    through :func:`save_block_atomic` or :func:`update_nonces_from_block`.
    """
    client.set(f"{NONCE_PREFIX}{pubkey}", nonce)


def update_nonces_from_block(client: Any, block: Block) -> None:
    """Increment the nonce for every sender in *block*.

    Called immediately after :func:`update_balances_from_block`.
    Each sender's nonce is set to ``tx.nonce + 1``, ensuring the next
    transaction from that sender must use a strictly greater nonce.
    """
    pipe = client.pipeline()
    for tx in block.transactions:
        pipe.set(f"{NONCE_PREFIX}{tx.sender_pubkey}", tx.nonce + 1)
    pipe.execute()


# ---------------------------------------------------------------------------
# Discarded transactions (audit M2)
# ---------------------------------------------------------------------------


def add_discarded_tx(client: Any, pubkey: str, tx_id: str) -> None:
    """Record *tx_id* as discarded for *pubkey*.
    Stored as a Redis Set so clients can discover why their transaction
    never appeared on-chain.
    """
    client.sadd(f"{DISCARDED_PREFIX}{pubkey}", tx_id)


def get_discarded_txns(client: Any, pubkey: str) -> list[str]:
    """Return tx_ids discarded for *pubkey*, newest first."""
    raw = client.smembers(f"{DISCARDED_PREFIX}{pubkey}")
    # decode_responses=True → smembers already returns str
    return list(raw)


def update_balances_from_block(client: Any, block: Block) -> None:
    """Atomically update the balance index for every transaction in *block*.

    Called from ``handle_result`` immediately after ``save_block``.
    Uses a Redis pipeline so all INCRBY commands are sent in one
    round-trip.  Not transactional across the full block (documented
    limitation).

    EARN → credits the student receiver.
    SPEND → debits the student sender.  Vendor receiver is intentionally
    *not* credited — vendors do not spend points in this domain.
    """
    pipe = client.pipeline()
    for tx in block.transactions:
        if tx.tx_type == "EARN":
            pipe.incrby(f"{BALANCE_PREFIX}{tx.receiver_pubkey}", tx.amount)
        elif tx.tx_type == "SPEND":
            pipe.incrby(f"{BALANCE_PREFIX}{tx.sender_pubkey}", -tx.amount)
    pipe.execute()


def rebuild_state_from_chain(client: Any) -> None:
    """Walk the full chain and recompute balances and nonces from scratch.

    Call at startup when the chain is non-empty but the balance index is
    missing (e.g. after a crash between ``save_block`` and
    ``update_balances_from_block``).
    """
    import logging
    logger = logging.getLogger(__name__)

    height = get_chain_height(client)
    logger.info("Rebuilding state from %d block(s)…", height)

    for i in range(height):
        block = get_block(client, i)
        if block is None:
            continue
        for tx in block.transactions:
            if tx.tx_type == "EARN":
                client.incrby(f"{BALANCE_PREFIX}{tx.receiver_pubkey}", tx.amount)
            elif tx.tx_type == "SPEND":
                client.incrby(f"{BALANCE_PREFIX}{tx.sender_pubkey}", -tx.amount)
            # Nonce: set to tx.nonce + 1 (last writer wins per sender)
            set_nonce(client, tx.sender_pubkey, tx.nonce + 1)

    logger.info("State rebuilt")


# ---------------------------------------------------------------------------
# Pending transaction pool persistence (audit L2)
# ---------------------------------------------------------------------------


def save_pending_tx(client: Any, tx: Transaction) -> None:
    """Append *tx* to the pending-transaction list in Redis.

    Called on every ``POST /transaction`` so that if the NCT crashes, the
    transaction is not lost — it is restored on the next startup.
    """
    client.rpush(PENDING_TXS_KEY, json.dumps(tx.to_dict(), sort_keys=True))


def restore_pending_txs(client: Any) -> list[Transaction]:
    """Return all transactions that were pending at the time of the last crash.

    Reads the full ``PENDING_TXS_KEY`` list, deserialises every entry, and
    returns them in insertion order (oldest first).  Returns an empty list
    when no pending transactions exist (fresh start or clean shutdown).
    """
    raw_list = client.lrange(PENDING_TXS_KEY, 0, -1)
    txs: list[Transaction] = []
    for raw in raw_list:
        try:
            txs.append(Transaction.from_dict(json.loads(raw)))
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Skipping unreadable pending tx: %s", raw[:100],
            )
    return txs


def trim_pending_txs(client: Any, count: int) -> None:
    """Remove the first *count* transactions from the pending list.

    Called after ``drain_pool_validated`` returns — the drained transactions
    are now in a block (mined or soon-to-be-mined) and should not be
    re-queued on restart.
    """
    if count <= 0:
        return
    # LTRIM key start stop: keeps elements from start to stop (0-based).
    # LTRIM count -1 removes the first *count* items.
    client.ltrim(PENDING_TXS_KEY, count, -1)


def clear_pending_txs(client: Any) -> None:
    """Remove all pending transactions (used during clean shutdown)."""
    client.delete(PENDING_TXS_KEY)


# Backward-compatible alias
rebuild_balances_from_chain = rebuild_state_from_chain
