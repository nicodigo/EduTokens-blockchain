"""Thread-safe shared state for the NCT orchestrator."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from shared.block import Block, Transaction


@dataclass
class NCTConfig:
    """NCT configuration read from environment variables."""

    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    worker_count: int = 2
    block_size: int = 5
    block_timeout: float = 30.0
    difficulty: int = 4
    nonce_space: int = 1_000_000_000
    max_nonce_space: int = 2**63 - 1  # audit L1: prevents unbounded growth
    port: int = 8080
    authority_pubkey: str = ""
    heartbeat_timeout: float = 15.0
    heartbeat_interval: float = 5.0
    pool_timeout: float = 60.0     # audit H2: seconds before pool considered dead
    rate_limit: str = "100/minute"


class NCTState:
    """Shared state between the three NCT threads.

    All mutable fields are protected by locks or threading primitives.
    """

    def __init__(self, worker_timeout: float = 15.0, pool_timeout: float = 60.0) -> None:
        # -- synchronisation primitives --
        self.lock = threading.Lock()
        self.tx_lock = threading.Lock()
        self.block_mined = threading.Event()
        self.shutdown = threading.Event()

        # -- current mining job (protected by self.lock) --
        self._current_block: Optional[Block] = None
        self._current_fingerprint: str = ""
        self._current_difficulty: int = 4
        self._current_nonce_space: int = 1_000_000_000
        self._mining_active: bool = False  # audit M2: decoupled from block_mined Event

        # -- transaction pool (protected by self.tx_lock) --
        self._tx_pool: list[Transaction] = []

        # -- worker registry (protected by self._worker_lock) --
        self._worker_lock = threading.Lock()
        self._workers: dict[str, float] = {}          # worker_id → last_seen
        self._worker_timeout: float = worker_timeout   # seconds before expiry

        # -- pool liveness (audit H2: prevents infinite loop when all pools dead) --
        self._pool_lock = threading.Lock()
        self._pool_last_seen: dict[str, float] = {}   # pool_id → last heartbeat time
        self._pool_no_workers: set[str] = set()        # pools that reported no workers
        self._pool_timeout: float = pool_timeout       # seconds before pool considered dead
        self._pools_ever_seen: bool = False            # True after first mark_pool_alive

        # -- chain height for /status (audit L4: reads and writes are
        #    best-effort — not protected by self.lock.  In CPython the
        #    GIL makes int assignment atomic, but the value seen by
        #    /status may be stale by one block during a concurrent
        #    handle_result call.  Acceptable for this PoC.) --
        self.chain_height: int = 0

    # ------------------------------------------------------------------
    # Current mining job
    # ------------------------------------------------------------------

    def set_current_block(self, block: Block, nonce_space: int) -> None:
        with self.lock:
            self._current_block = block
            self._current_fingerprint = block.fingerprint
            self._current_difficulty = block.difficulty
            self._current_nonce_space = nonce_space
            self._mining_active = True
            self.block_mined.clear()

    def get_current_for_verification(self) -> tuple[Optional[Block], str, int]:
        """Snapshot for result-loop verification (avoids holding lock)."""
        with self.lock:
            return (
                self._current_block,
                self._current_fingerprint,
                self._current_difficulty,
            )

    def get_current_nonce_space(self) -> int:
        with self.lock:
            return self._current_nonce_space

    def mining_active(self) -> bool:
        """Return ``True`` while the NCT is actively waiting for a PoW solution.

        Audit M2: this is a dedicated flag, decoupled from the ``block_mined``
        Event which is used solely to signal the ``block_loop`` thread.
        """
        with self.lock:
            return self._mining_active

    def mark_mining_complete(self) -> None:
        """Signal that the current block has been successfully mined and persisted.

        Audit M2: combines setting ``_mining_active = False`` and firing the
        ``block_mined`` Event into a single atomic operation under ``self.lock``,
        preventing the semantic overloading of ``block_mined`` for duplicate
        guarding.
        """
        with self.lock:
            self._mining_active = False
            self.block_mined.set()

    # ------------------------------------------------------------------
    # Transaction pool
    # ------------------------------------------------------------------

    def add_transaction(self, tx: Transaction) -> None:
        with self.tx_lock:
            self._tx_pool.append(tx)

    def drain_pool(self, max_count: int) -> list[Transaction]:
        with self.tx_lock:
            taken = self._tx_pool[:max_count]
            self._tx_pool = self._tx_pool[max_count:]
            return taken

    def pool_size(self) -> int:
        with self.tx_lock:
            return len(self._tx_pool)

    def pool_snapshot(self) -> list[Transaction]:
        with self.tx_lock:
            return list(self._tx_pool)

    # ------------------------------------------------------------------
    # Worker registry (keep-alive)
    # ------------------------------------------------------------------

    def update_worker(self, worker_id: str) -> None:
        """Record a heartbeat from *worker_id*."""
        with self._worker_lock:
            self._workers[worker_id] = time.time()

    def get_active_worker_count(self) -> int:
        """Return the number of workers that have sent a heartbeat recently."""
        cutoff = time.time() - self._worker_timeout
        with self._worker_lock:
            stale = [wid for wid, ts in self._workers.items() if ts < cutoff]
            for wid in stale:
                del self._workers[wid]
            return len(self._workers)

    def active_workers_snapshot(self) -> list[str]:
        """Return a list of active worker IDs (for debugging)."""
        cutoff = time.time() - self._worker_timeout
        with self._worker_lock:
            return sorted(
                wid for wid, ts in self._workers.items() if ts >= cutoff
            )

    # ------------------------------------------------------------------
    # Pool liveness (audit H2 — prevents infinite loop when all pools dead)
    # ------------------------------------------------------------------

    def mark_pool_alive(self, pool_id: str) -> None:
        """Record a heartbeat (or any non-``pool_no_workers`` message) from *pool_id*."""
        with self._pool_lock:
            self._pool_last_seen[pool_id] = time.time()
            self._pool_no_workers.discard(pool_id)
            self._pools_ever_seen = True

    def mark_pool_dead(self, pool_id: str) -> None:
        """Record that *pool_id* reported it has no workers."""
        with self._pool_lock:
            self._pool_no_workers.add(pool_id)

    def active_pools(self) -> int:
        """Return the number of pools with a recent heartbeat that have NOT
        reported ``pool_no_workers``."""
        cutoff = time.time() - self._pool_timeout
        with self._pool_lock:
            # Clean up stale entries
            stale = [pid for pid, ts in self._pool_last_seen.items() if ts < cutoff]
            for pid in stale:
                del self._pool_last_seen[pid]
                self._pool_no_workers.discard(pid)
            # If all entries were cleaned up but we've seen pools, they're all dead
            if not self._pool_last_seen and self._pools_ever_seen:
                return 0
            # Active = seen recently AND not marked dead
            return sum(
                1 for pid in self._pool_last_seen
                if pid not in self._pool_no_workers
            )

    def all_pools_dead(self) -> bool:
        """Return ``True`` when every known pool is either timed out or
        explicitly reported ``pool_no_workers``.

        Returns ``False`` when no pools have ever been seen (unknown state).
        """
        with self._pool_lock:
            if not self._pools_ever_seen:
                return False  # unknown — no pools tracked yet
            cutoff = time.time() - self._pool_timeout
            # Clean up stale entries inline (same logic as active_pools)
            stale = [pid for pid, ts in self._pool_last_seen.items() if ts < cutoff]
            for pid in stale:
                del self._pool_last_seen[pid]
                self._pool_no_workers.discard(pid)
            return sum(
                1 for pid in self._pool_last_seen
                if pid not in self._pool_no_workers
            ) == 0
