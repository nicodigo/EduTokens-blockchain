# Audit P6 — Integration & Cross-Component Consistency

**Scope:** Message flow symmetry (all routing keys), state machine global correctness, component startup/shutdown ordering, compound edge cases (multi-component failures), transaction lifecycle audit

**Audit date:** 2026-06-18

**Note:** This phase analyses how components interact. Findings from P1–P5 are cross-referenced where relevant; new findings are those only visible at the integration level.

**Status:** FIXED

---

## Findings

### HIGH

#### H1 — Pool monitor gap: stale monitor exits with no replacement when tasks arrive rapidly

- **File:** `pilar2/pool/pool.py:252-257` (monitor creation), `pilar2/pool/pool.py:327-370` (monitor loop)
- **Fragment:**
  ```python
  # _on_mining_task (each new NCT broadcast):
  self._monitor_active.set()                                    # line 252
  if self._monitor_thread is None or not self._monitor_thread.is_alive():  # line 253
      self._monitor_thread = threading.Thread(target=self._monitor_loop, ...)
      self._monitor_thread.start()                               # line 257
  # ← New thread NOT started if old one is still alive
  ```
  ```python
  # _monitor_loop:
  current_block_index = self._current_block_index  # line 319 — captured at start
  while self._monitor_active.is_set():
      self._monitor_active.wait(timeout=5)          # line 328
      if not self._monitor_active.is_set():
          return                                    # line 330 — exits
  ```
- **Scenario:** When the NCT re-publishes a mining task (timeout expansion) while the old monitor is still alive:
  1. First `task.mining` arrives → monitor started for block N.
  2. NCT times out, republishes `task.mining` for block N (expanded range).
  3. Pool receives second `task.mining` → `_on_mining_task` sets `_monitor_active.set()`, but `_monitor_thread.is_alive()` is `True` (old monitor still running) → **no new monitor thread is started**.
  4. Old monitor eventually checks `_monitor_active` → still set → continues monitoring. But it's using the **first** task's `_original_worker_count` (from line 252, set by the new task — actually overwritten). The monitor re-publishes with the new `self._current_block_index` and `self._current_nonce_space` (live values), which accidentally works.

  **The gap:** If instead a **different** block's task arrives (block N+1) while the old monitor for block N is still running:
  1. Block N result arrives → `_monitor_active.clear()` (line 295), `_current_block_index = None` (line 296).
  2. Old monitor wakes from `wait(5)`, sees `_monitor_active` cleared → exits (line 330).
  3. **TOCTOU race:** Between old monitor's `return` and the thread actually dying, step 5 below executes.
  4. Block N+1 `task.mining` arrives → sets `_current_block_index = N+1`, `_monitor_active.set()`.
  5. `_monitor_thread.is_alive()` → `True` (old thread not yet reaped) → **no new monitor**.
  6. Old monitor dies a millisecond later. **No monitor for block N+1.**

  If a worker dies during block N+1 mining, its sub-range is **never re-published**.  The pool waits for the worker's heartbeat to expire (15s) and re-assigns on the NEXT task — but while mining block N+1, dead workers' ranges go unhandled until the NCT's global timeout expansion.

- **Recommendation:** Track monitor generation with a counter.  Pass the expected `block_index` to the monitor.  In `_on_mining_task`, always start a fresh monitor thread and signal the old one to exit (via a dedicated `threading.Event`).  Example:
  ```python
  # In _on_mining_task:
  self._monitor_stop.set()          # signal old monitor
  if self._monitor_thread:
      self._monitor_thread.join(timeout=1)
  self._monitor_stop.clear()
  self._monitor_thread = threading.Thread(target=self._monitor_loop, args=(task.block_index,), ...)
  self._monitor_thread.start()
  ```

---

#### H2 — NCT receives `pool_no_workers` signal but only logs it; chain stalls silently

- **File:** `pilar2/nct/nct.py:350-354` (NCT handler), `pilar2/pool/pool.py:360-366` (pool publisher)
- **Fragment (NCT):**
  ```python
  if data.get("action") == "pool_no_workers":
      logger.warning(
          "Pool '%s' reports no workers for block %s",
          data.get("worker_id"), data.get("block_index"),
      )
      # ← No action taken
  ```
  **Fragment (Pool):**
  ```python
  self._channel.basic_publish(
      exchange=EXCHANGE,
      routing_key=f"worker.{self.pool_id}.status",
      body=json.dumps({
          "worker_id": self.pool_id,
          "action": "pool_no_workers",
          "block_index": self._current_block_index,
          ...
      }),
  )
  ```
- **Risk:** When all workers in a pool die, the pool publishes a `pool_no_workers` status message to the NCT.  The NCT receives it, logs a warning — and **does nothing else**.  The block_loop's `block_mined.wait(timeout=...)` will expire, and the NCT will double `nonce_space` and re-publish `task.mining`.  The pool receives it but has no workers to assign to.  The pool re-publishes another `pool_no_workers`.  The cycle repeats — the chain is stalled permanently with no alert, no escalation, no automatic recovery.

  The `pool_no_workers` message semantically means "I cannot process this block."  A reasonable response would be for the NCT to stop mining tasks to this pool (e.g., track which pools are alive), or to escalate (log CRITICAL, trigger alert).  Currently it's a no-op.

- **Recommendation:** Either (a) have the NCT track pool liveness and stop publishing to dead pools, (b) escalate the log level to `ERROR` and add an alert threshold (e.g., `pool_no_workers` received 3 times → CRITICAL), or (c) implement a global timeout that abandons the block after N expansions and moves to the next block (losing the transactions, but keeping the chain moving).

---

#### H3 — `task.mining` broadcast architecture: all pools compete wastefully, no work coordination

- **Files:** `pilar2/nct/nct.py:300-306` (publish), `pilar2/pool/pool.py:131-132` (bind)
- **Routing key:** `task.mining` — published once by NCT, bound by ALL pools via fanout.
- **Risk:** The current architecture is **competitive**, not cooperative.  Every pool receives every mining task.  All pools partition their nonce space and mine independently.  The first pool to find a solution wins; all other pools' work is wasted.  For a PoC with 1 pool this is fine.  With 2 pools, 50% of global compute is wasted.  With N pools, (N-1)/N of compute is wasted.

  This is a deliberate design choice (documented in `project_overview.md`), but it has operational consequences not addressed in the code:
  - There is no mechanism for pools to coordinate nonce ranges (e.g., pool A mines [0, 5e8], pool B mines [5e8, 1e9]).
  - The NCT does not track how many pools are active — it broadcasts blindly.
  - Adding more pools does not increase throughput; it only increases redundancy.

- **Recommendation:** Accept the competitive model for the PoC.  For production, implement a cooperative model where the NCT partitions the nonce space among pools (similar to how pools partition among workers) and publishes per-pool tasks.

---

### MEDIUM

#### M1 — Pool worker heartbeats received by both Pool and NCT; NCT accumulates stale state

- **Files:** `pilar2/worker/worker.py:188` (publish), `pilar2/pool/pool.py:147-148` (pool bind), `pilar2/broker/broker.py:93-94` (NCT bind)
- **Routing key flow:**
  ```
  Worker: worker.{pool_id}.heartbeat
    ├── Pool's registry: worker.{pool_id}.*  → used for dynamic worker count  ✓
    └── NCT's registry:  worker.*            → registers as a generic worker   ← unbounded growth
  ```
- **Risk:** Pool worker heartbeats match `worker.*` and are consumed by the NCT's `WORKER_REGISTRY_QUEUE`.  The NCT's `update_worker()` at nct.py:356 adds them to `self._workers` dict.  These workers are never used by the NCT (the NCT doesn't assign tasks to individual pool workers).  The `_workers` dict grows unbounded — every pool worker that ever sent a heartbeat remains in the dict until it's evicted by `get_active_worker_count()` (nct/state.py:119-124), which only runs when the NCT **polls** the worker registry queue (in `result_loop`).  But if no new heartbeats arrive, `get_active_worker_count()` is never called, and stale workers accumulate.

  In practice, `get_active_worker_count()` is called from `result_loop` line 356 only when a new heartbeat arrives.  Stale entries are evicted lazily.  With 2 pool workers, this is negligible.  With hundreds, it's a memory leak.

- **Recommendation:** Add periodic cleanup in the NCT's result_loop (e.g., every 60s, call `state.get_active_worker_count()` regardless of whether a new heartbeat arrived).  Or use a more specific routing key pattern for pool worker heartbeats (e.g., `worker.pool.{pool_id}.heartbeat`) that doesn't match `worker.*` on the NCT side.

---

#### M2 — `block_mined` Event semantically overloaded: signal + duplicate guard + stale check

- **File:** `pilar2/nct/nct.py:186,222,312`, `pilar2/nct/state.py:70`
- **Usage of `block_mined`:**
  | Context | Operation | Purpose |
  |---------|-----------|---------|
  | `set_current_block` (state.py:70) | `clear()` | Reset for new block |
  | `handle_result` guard (nct.py:186) | `is_set()` | Duplicate result rejection |
  | `handle_result` completion (nct.py:222) | `set()` | Signal block_loop |
  | `block_loop` wait (nct.py:312) | `wait(timeout)` | Block until mined or timeout |
- **Risk:** A single `threading.Event` serves three distinct purposes: (1) signaling the block_loop that mining is complete, (2) acting as a duplicate-result guard in `handle_result`, and (3) implicit stale check (cleared in `set_current_block`).  This overloading makes the state machine harder to reason about.  For example, `handle_result` line 186 checks `is_set()` BEFORE `get_current_for_verification()` — so the duplicate guard relies on the Event being set in a prior `handle_result` call.  If `handle_result` is called in a loop (as it is in `result_loop`), this works.  But if called out of order, it could accept duplicate results.

  No actual bug observed, but the overloading is a maintenance risk.

- **Recommendation:** Separate into two primitives: `block_complete: Event` (for signaling block_loop) and `_mining_active: bool` (for duplicate guard, protected by `lock`).

---

#### M3 — Dynamic worker count starts at 0 until first heartbeat; fallback graceful but initial task mis-sized

- **File:** `pilar2/pool/pool.py:187-200` (`_get_active_worker_count`), `pilar2/pool/pool.py:230-246` (usage in `_on_mining_task`)
- **Fragment:**
  ```python
  def _get_active_worker_count(self) -> int:
      # … count workers with recent heartbeats …
      if count == 0:
          if self._worker_count_fallback == 0:
              return 1          # never return 0 (avoids division by zero)
          return self._worker_count_fallback
      return active_count
  ```
- **Risk:** At startup, before any worker sends its first heartbeat, `_get_active_worker_count()` returns the fallback (`POOL_WORKER_COUNT=2`).  This is correct.  But: the same fallback is returned if ALL workers are dead and their heartbeats expired.  `original_worker_count` at line 252 is set to this fallback value.  The monitor then uses this count.  If the fallback is 2 but only 1 worker is actually active, the monitor expects 2 and re-publishes when only 1 is alive → duplicate work for the surviving worker.

  The real issue: the pool has no way to know the "true" number of workers.  It relies entirely on heartbeats.  If a worker starts but its heartbeat connection fails (P4-H4), it never registers as active, and the pool assigns zero tasks to it.

- **Recommendation:** Add a worker registration message (not just heartbeat) that the worker sends once at startup on a dedicated routing key.  The pool should refuse to start mining until it has registered workers (with a configurable timeout before falling back).

---

#### M4 — No dead-letter queue; unrouteable messages silently dropped

- **Files:** All `basic_publish` calls (broker.py, worker.py, pool.py)
- **Risk:** RabbitMQ's topic exchange silently drops messages published to a routing key with no bound queue.  Combined with the lack of `mandatory=True` (P2-L1), misrouted messages vanish without any log, error, or trace.  A configuration error (e.g., worker binds to `pool-a.task.*` but pool publishes to `pool-b.task.*`) would cause all mining tasks for that pool to silently disappear.

- **Recommendation:** Configure a Dead Letter Exchange (DLX) on all durable queues.  Messages that are rejected (nack without requeue) or expire are routed to the DLX, where they can be inspected and re-published.  For the PoC, at minimum add `mandatory=True` to critical publishes and log returned messages.

---

### LOW

#### L1 — `nonce_space` doubling has no upper bound

- **File:** `pilar2/nct/nct.py:318`
- **Fragment:**
  ```python
  nonce_space *= 2   # No ceiling
  ```
- **Risk:** With `BLOCK_TIMEOUT=30` and `NONCE_SPACE=1e9`, after 10 timeouts (~5 minutes), `nonce_space = 1e12` (1 trillion).  After 20 timeouts (~10 minutes), `nonce_space = 1e15`.  At this point, the CPU miner would take years.  There's no cap.  The pool's `publish_tasks` (`broker.py:116`) partitions `range_size // num_workers` — if `range_size` exceeds 2^63 - 1, Python handles it (arbitrary precision) but the CUDA miner (`md5_range` compiled with C `int` or `long long`) may not.

  In practice, with `difficulty=4`, finding a solution requires ~2^16 = 65K attempts on average (because MD5 is 128 bits, difficulty=4 means 2^16 expected).  With `nonce_space=1e9`, the probability of finding a solution is ~1.0.  If no solution is found, it means something is wrong (e.g., difficulty > 8, bug in the miner, no workers).  Doubling forever won't help.

- **Recommendation:** Add a maximum (e.g., `nonce_space <= 2**63 - 1`) and after reaching it, log a CRITICAL error and either restart with a new block (losing transactions) or halt gracefully.

---

#### L2 — Transaction pool is in-memory only; lost on NCT restart

- **File:** `pilar2/nct/state.py:54` (`_tx_pool: list`)
- **Risk:** The transaction pool lives in Python memory.  On NCT restart (crash, deploy, container restart), all pending transactions are **lost**.  Clients received HTTP 201 but their transactions never appeared on-chain.  Combined with P3-M2 (silently discarded transactions), clients have no way to know their transaction was lost.  The pool is not persisted to Redis.

- **Recommendation:** Persist the transaction pool to Redis (e.g., a separate Redis List `blockchain:pending_txs`).  On startup, restore from Redis.  This also enables horizontal scaling (multiple NCT instances sharing the pool).

---

#### L3 — `pool_no_workers` reuses `worker.*` routing key; fragile `action` field differentiation

- **File:** `pilar2/pool/pool.py:360-366` (publish), `pilar2/nct/nct.py:349-356` (consume)
- **Fragment:**
  ```python
  # Pool publishes:
  routing_key=f"worker.{self.pool_id}.status"
  body={"worker_id": self.pool_id, "action": "pool_no_workers", ...}

  # NCT parses:
  data = json.loads(body.decode())
  if data.get("action") == "pool_no_workers":
      # ... handle
  else:
      state.update_worker(data.get("worker_id", "unknown"))  # treats as heartbeat
  ```
- **Risk:** The differentiation between "pool status" and "worker heartbeat" relies entirely on the `action` field in the JSON body.  Both use `worker.*` routing keys.  If a heartbeat message format ever adds an `action` field (e.g., `{"action": "heartbeat", ...}`), the NCT's if/else would correctly branch.  But if a bug causes a heartbeat to have `action: "pool_no_workers"`, it would be misinterpreted as a pool status.  Conversely, if the pool status message changes its `action` field, it would be treated as a heartbeat and `update_worker` would register the pool_id as a worker.

- **Recommendation:** Use separate routing keys: `pool.{id}.status` for pool status messages, `worker.*` for heartbeats only.  Bind a separate queue on the NCT side.

---

### INFO

#### I1 — Routing key symmetry matrix (all patterns verified)

| # | Routing Key Pattern | Publisher | Consumer(s) | Queue(s) | Matches? |
|---|---------------------|-----------|-------------|----------|----------|
| 1 | `task.mining` | NCT `publish_mining_task` | Pool inbox, Worker solo inbox | `pool.{id}.inbox`, `worker.{id}.inbox` | ✅ |
| 2 | `pool.{id}.task.{n}` | Pool `publish_tasks` | Worker (pool mode) | `pool.{id}.tasks` | ✅ |
| 3 | `pool.{id}.result.{wid}` | Worker (pool mode) | Pool | `pool.{id}.results` | ✅ |
| 4 | `result.{pool_id}` | Pool (forward) | NCT | `mining_results` | ✅ |
| 5 | `result.{worker_id}` | Worker (solo) | NCT | `mining_results` | ✅ |
| 6 | `worker.{pool_id}.heartbeat` | Worker (pool mode) | Pool, NCT | `pool.{id}.registry`, `worker_registry` | ✅ |
| 7 | `worker.heartbeat` | Worker (solo) | NCT | `worker_registry` | ✅ |
| 8 | `worker.{pool_id}.status` | Pool (no-workers) | NCT | `worker_registry` | ✅ via `worker.*` |
| 9 | `control` | NCT `broadcast_abort`, Pool `_broadcast_abort` (global) | Worker (all) | Anonymous (exclusive) | ✅ |
| 10 | `pool.{id}.control` | Pool `_broadcast_abort` (local) | Worker (pool mode) | Anonymous (exclusive) | ✅ |
| 11 | `task.{n}` | Dead code default | **None** | — | ❌ P2-M2 |

**Verdict:** All 10 active routing key patterns have matching consumers.  No orphaned publishers or subscribers.  The dead default `task.{n}` (row 11) is a documented footgun (P2-M2) but is never invoked in production.

---

#### I2 — Transaction lifecycle trace: 2 confirmed loss points

```
Client POST /transaction
  │
  ├─ [1] Pydantic validation                    ← 422 if malformed
  ├─ [2] Structural validation (t.validate())    ← 400 if invalid
  ├─ [3] Signature verification (Ed25519)       ← 400 if bad sig; 500 if bad pubkey (P1-C1)
  ├─ [4] Nonce check (Redis)                    ← 400 if stale
  ├─ [5] Authority check (EARN only)            ← 400 if not authority
  │
  ├─ state.add_transaction(t)                   ← IN MEMORY POOL
  │
  │  ⚠️ LOSS POINT A: NCT crashes here → tx in memory, lost forever
  │
  ├─ block_loop drains pool via accumulate_transactions → drain_pool_validated
  │  ├─ ⚠️ LOSS POINT B: Discarded for insufficient balance or stale nonce → silently lost (P3-M2)
  │  └─ Accepted → included in Block
  │
  ├─ Block published to task.mining
  ├─ Workers mine → Pool verifies → result forwarded
  ├─ NCT result_loop: verify PoW → save_block → update_balances → update_nonces
  │
  └─ ✅ PERSISTED in Redis (blockchain:blocks)
```

---

#### I3 — State machine: 2 unrecoverable states identified

| State | Trigger | Behavior | Recovery |
|-------|---------|----------|----------|
| **Redis down** | Redis crash during `handle_result` | `save_block` raises → result_loop crashes (P3-H4) → block_loop keeps publishing, workers keep mining, results accumulate in RabbitMQ → blockchain frozen | Manual: restart Redis, then restart NCT (balance rebuild on startup) |
| **All workers dead** | Workers crash or heartbeat timeout | Pool publishes `pool_no_workers` → NCT logs warning → block_loop times out → expands nonce_space → re-publishes → still no workers → loop | Manual: restart workers |
| **RabbitMQ down** | RabbitMQ crash | All channels crash (P2-H2) → all daemon threads crash → health servers keep running (200 OK) → blockchain frozen | Manual: restart RabbitMQ, then restart all services |
| **Single worker dead** | Worker crash | Pool monitor detects (5-15s) → re-publishes orphaned sub-ranges (P4-M1) | ✅ Auto-recovery |

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| HIGH | 3 | H1 — Pool monitor gap on rapid task arrival, H2 — `pool_no_workers` signal ignored, H3 — Competitive pool architecture wastes (N-1)/N compute |
| MEDIUM | 4 | M1 — NCT accumulates stale pool worker state, M2 — `block_mined` Event overloaded, M3 — Dynamic worker count starts at 0, M4 — No dead-letter queue |
| LOW | 3 | L1 — `nonce_space` unbounded, L2 — In-memory tx pool lost on restart, L3 — Fragile `action` field differentiation |
| INFO | 3 | I1 — Routing key symmetry matrix (11 patterns, 10 matched), I2 — Transaction lifecycle trace (2 loss points), I3 — State machine unrecoverable states (3 identified, 1 auto-recovered) |

**Most impactful fix:** H1 (pool monitor gap) — combined with P4-C1 (abort doesn't work), a dead worker during mining leaves its sub-range permanently unprocessed.  The pool relies on the monitor as its sole dead-worker recovery mechanism; if the monitor isn't running for the current block, worker deaths go undetected.
