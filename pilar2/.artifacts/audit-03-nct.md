# Audit P3 — NCT Orchestrator

**Scope:** `pilar2/nct/nct.py` (585 lines), `pilar2/nct/state.py` (115 lines)

**Audit date:** 2026-06-18

---

## Findings

### CRITICAL

#### C1 — `BlockingChannel` shared across block_loop and result_loop threads

- **File:** `pilar2/nct/nct.py:542-543` (channel creation), `pilar2/nct/nct.py:554-558` (thread creation)
- **Fragment (main):**
  ```python
  channel = rmq_conn.channel()               # line 543 — ONE channel

  threads = [
      threading.Thread(target=block_loop, args=(..., channel, ...), ...),   # line 555
      threading.Thread(target=result_loop, args=(..., channel), ...),       # line 557
  ]
  ```
  **block_loop** publishes on this channel via `publish_mining_task()` → `channel.basic_publish()` (broker.py:164).
  **result_loop** consumes on this channel via `channel.basic_get()` (nct.py:336,345).
- **Risk:** pika's `BlockingChannel` is **not thread-safe** — its documentation states: "Channels are not thread safe. [...] Share connections, not channels."  Using the same channel from two threads (one publishing, one consuming) produces undefined behavior:
  - Concurrent `basic_publish` and `basic_get` can corrupt the channel's internal frame buffer.
  - Symptoms include: out-of-order frames, dropped messages, deadlocks in the AMQP state machine, or `StreamLostError` at unpredictable times.
  - The Python GIL prevents true parallel execution, but thread context switches mid-AMQP-frame can still desynchronise the channel state.
  - This is a **deterministic bug** — it will manifest under load.
- **Recommendation:** Create two separate channels from the same connection:
  ```python
  channel = rmq_conn.channel()
  pub_channel = rmq_conn.channel()        # for block_loop
  sub_channel = rmq_conn.channel()        # for result_loop
  ```
  Pass `pub_channel` to `block_loop`, `sub_channel` to `result_loop`.  pika `BlockingConnection` supports multiple channels.

---

#### C2 — Unhandled `ValueError` from Ed25519 pubkey parsing → 500 crash

- **File:** `pilar2/nct/nct.py:420`
- **Fragment:**
  ```python
  if not crypto_verify(t.sender_pubkey, t.tx_id.encode(), t.signature):
  ```
- **Risk:** As documented in **Audit P1-C1** (`audit-01-models.md`), `crypto_verify()` calls `Ed25519PublicKey.from_public_bytes()` which raises `ValueError` for invalid curve points.  This exception is not caught in the POST handler → FastAPI returns 500 Internal Server Error.  The Pydantic validation only checks hex charset and length, not curve membership.  An attacker can craft a 64-hex-char pubkey that passes Pydantic but crashes the endpoint.
- **Recommendation:** (Same as P1-C1) Fix `crypto.py:verify()` to catch `ValueError` and return `False`.  Alternatively, add a `try/except ValueError` in the POST handler at line 420.

---

### HIGH

#### H1 — `auto_ack=True` loses messages on processing failure

- **File:** `pilar2/nct/nct.py:336-338,345-347`
- **Fragment:**
  ```python
  method, _properties, body = channel.basic_get(
      queue=RESULTS_QUEUE, auto_ack=True,        # line 337
  )
  # … process body …
  # If handle_result crashes (Redis down, etc.), message is already acked → LOST
  ```
- **Risk:** With `auto_ack=True`, RabbitMQ removes the message from the queue **before** `handle_result()` processes it.  If processing fails — Redis connection drops during `save_block()` (line 215), the block assembly is inconsistent, or any unhandled exception — the message (a valid PoW solution from a worker) is **gone forever**.  The worker already moved on; it won't re-send.  The block_loop times out and expands the nonce space, requiring a new PoW for the same block from scratch.

  The same applies to `WORKER_REGISTRY_QUEUE` (line 346) — heartbeats and pool-no-workers alerts are lost if JSON parsing fails.

- **Recommendation:** Use `auto_ack=False` and call `channel.basic_ack(method.delivery_tag)` only after successful processing.  Example:
  ```python
  method, _, body = channel.basic_get(queue=RESULTS_QUEUE, auto_ack=False)
  if method and body:
      try:
          result = ResultMessage.from_json(body.decode())
          handle_result(state, redis_client, channel, result)
          channel.basic_ack(delivery_tag=method.delivery_tag)
      except Exception:
          logger.exception("Failed to process result — not acking for redelivery")
  ```

---

#### H2 — Daemon threads killed without cleanup on shutdown

- **File:** `pilar2/nct/nct.py:554-560`
- **Fragment:**
  ```python
  threading.Thread(target=block_loop, ..., daemon=True),     # line 555
  threading.Thread(target=result_loop, ..., daemon=True),    # line 557
  threading.Thread(target=health_loop, ..., daemon=True),    # line 559
  ```
- **Risk:** `daemon=True` threads are forcibly terminated when the main thread exits — they receive no chance to flush buffers, close connections, or save state.  On shutdown (SIGINT/SIGTERM):
  - Transactions in the pool are **lost** (in-memory only, never persisted).
  - RabbitMQ channels/connections are not cleanly closed — broker sees abrupt TCP RST.
  - The mining task for the current block remains in-flight; workers keep mining a block that will never be completed.
  - `uvicorn.run()` in the health_loop is a blocking call that doesn't check `shutdown` flag — it's killed mid-request.

  The `shutdown` event is only checked in polling loops (`block_loop`, `result_loop`), not in the health_loop.  Even the block_loop checks `shutdown` only at the top of its outer `while` loop — if it's sleeping in `block_mined.wait(timeout=...)`, it won't notice shutdown until that sleep expires.

- **Recommendation:** (a) Use non-daemon threads with explicit `join()` in the shutdown handler; (b) replace `uvicorn.run()` with `uvicorn.Server` and call `server.should_exit = True` in the signal handler; (c) drain and log the transaction pool before exit; (d) close RabbitMQ connection gracefully (`rmq_conn.close()`).

---

#### H3 — `state.chain_height` initialised to 1, ignoring existing chain on restart

- **File:** `pilar2/nct/nct.py:548`
- **Fragment:**
  ```python
  state.chain_height = 1  # genesis is block 0 → height = 1
  ```
- **Risk:** If the NCT restarts with an existing chain (e.g. 5 blocks in Redis), `chain_height` is set to 1.  The `/status` endpoint reports `chain_height: 1` until a new block is mined, at which point `handle_result` sets it to `current_block.index + 1` (= 6).  Between restart and the next mined block, the API reports **stale data**.  This affects monitoring, dashboards, and any logic that depends on `/status`.

- **Recommendation:** Read actual chain height from Redis after `ensure_genesis()`:
  ```python
  state.chain_height = get_chain_height(redis_client)
  ```
  This correctly handles both fresh starts (height=1 after genesis) and restarts (height=N).

---

#### H4 — No error recovery in `result_loop`; crash freezes mining permanently

- **File:** `pilar2/nct/nct.py:324-362`
- **Fragment:** The entire `result_loop` body has **zero** try/except around the polling loop.
  ```python
  while not state.shutdown.is_set():
      # basic_get calls + handle_result — any exception propagates out
  ```
- **Risk:** If `basic_get` raises `StreamLostError` (RabbitMQ reconnect needed — see Audit P2-H2) or `handle_result` raises any exception (e.g. Redis `ConnectionError` during `save_block`), the exception propagates out of the loop, crashing the daemon thread silently.  The `block_loop` keeps running, publishes new mining tasks, but no results are ever processed — `block_mined` is never set.  The block_loop times out, doubles nonce_space, republishes, times out again, endlessly.

  The system appears healthy: health_loop still serves `/health` (200 OK), but the blockchain is **frozen**.  No alert, no auto-recovery.

- **Recommendation:** Wrap the entire loop body in `try: ... except Exception:` with logging and a brief sleep before retry.  For connection-level errors, re-establish the channel/connection.

---

### MEDIUM

#### M1 — Three independent PoW verification implementations; DRY violation and consistency risk

- **Files:**
  - `pilar2/nct/nct.py:89-101` (`verify_pow_result`)
  - `pilar2/shared/block.py:314-325` (`Block.verify_pow`)
  - `pilar2/pool/pool.py:378-387` (`_verify`)
- **Fragment (nct version):**
  ```python
  def verify_pow_result(fingerprint, difficulty, nonce, claimed_hash):
      pow_hash = hashlib.md5((fingerprint + str(nonce)).encode()).hexdigest()
      valid = (pow_hash == claimed_hash) and pow_hash.startswith("0" * difficulty)
      return valid, pow_hash
  ```
- **Divergence:** The NCT version checks `pow_hash == claimed_hash` **and** difficulty.  The Block version (`Block.verify_pow`) checks **only** difficulty — it does not verify the claimed hash matches the computed hash.  The Pool version (`pool.py:378`) is similar but subtly different.  If the verification logic needs updating (e.g. switching to SHA-256 PoW), three places must be changed.  One will be missed.
- **Recommendation:** Consolidate into a single implementation in `shared/block.py` (the `Block.verify_pow` static method, enhanced to also check claimed hash).  Have the NCT and Pool import and use it.

---

#### M2 — Discarded transactions at assembly time vanish silently; no client notification

- **File:** `pilar2/nct/nct.py:105-171` (`drain_pool_validated`)
- **Fragment:**
  ```python
  discarded: list[Transaction] = []
  # ... transactions that fail nonce or balance checks are appended to discarded ...
  if discarded:
      logger.info("%d transacción(es) descartada(s) por saldo insuficiente", len(discarded))
  # discarded list is never returned or exposed to the API
  ```
- **Risk:** Clients receive HTTP 201 at POST time (the transaction was "accepted" into the pool), but if the transaction is later discarded at assembly time (insufficient balance or stale nonce), the client has **no way to know**.  The `/account/{pubkey}` endpoint returns the next expected nonce from Redis, which only updates after a block is mined — so even after mining, the client sees the nonce unchanged and thinks their transaction was never processed.  They may re-submit, creating duplicate work.

- **Recommendation:** Store discarded transaction IDs in a Redis set (`discarded_txns`) that the `/account/{pubkey}` endpoint can check.  Or expose a `/transaction/{tx_id}/status` endpoint.  At minimum, log at WARNING level with enough detail for debugging.

---

#### M3 — No upper bound on `amount`; infinite or absurdly large values accepted

- **File:** `pilar2/nct/nct.py:404` (Transaction creation from request)
- **Risk:** Pydantic validates `amount > 0`, but there's no maximum.  `float('inf')` passes?  Actually Pydantic's `gt=0` rejects `inf` (because `inf > 0` is True but `inf` is not a finite number... actually, Pydantic uses Python comparison, and `float('inf') > 0` is `True`.  Pydantic's `gt` does NOT reject `inf` by default).  A student could `EARN` infinity tokens.  Even finite large amounts like `1e308` would cause JSON serialization to output `1e+308`, which could cause issues in `INCRBYFLOAT` (Redis uses IEEE 754 doubles — cap at ~1.79e308).  More realistically, `1e20` tokens could cause display issues and overflow the balance overlay logic.

- **Recommendation:** Add `le=1_000_000_000` or similar to `TransactionRequest.amount` in schemas.py, and add validation in `Transaction.validate()`.  Define a token cap based on the domain (e.g. university annual budget).

---

#### M4 — `/chain` endpoint returns full chain without pagination

- **File:** `pilar2/nct/nct.py:470-479`
- **Fragment:**
  ```python
  @app.get("/chain", response_model=list[dict])
  def get_chain() -> list[dict]:
      height = get_chain_height(redis_client)
      result: list[dict] = []
      for i in range(height):
          blk = get_block(redis_client, i)          # N Redis queries
          if blk is not None:
              result.append(blk.to_dict())
      return result
  ```
- **Risk:** For N blocks, this endpoint makes N Redis `LINDEX` calls (O(N) latency) and returns the entire chain in a single JSON response.  With 1000 blocks (1 transaction/block at 5 tx/block = 200 blocks ≈ easily reachable in a demo), the response is ~1 MB and takes ~1000 Redis round-trips.  Repeated calls could degrade `/status` and `/health` latency by saturating Redis.  No rate limiting.

- **Recommendation:** Add `?start=N&count=M` query parameters with sane defaults (e.g. last 20 blocks).  Use Redis `LRANGE` for O(1) range queries instead of N `LINDEX` calls.

---

#### M5 — `ensure_genesis` recovery heuristic excludes partial nonce corruption

- **File:** `pilar2/nct/nct.py:510-515`
- **Risk:** As documented in Audit P2-L3, the recovery condition `not balance_keys or not nonce_keys` only triggers when ALL balance keys or ALL nonce keys are missing.  It does NOT detect partial corruption (e.g. one account's nonce is stale after a crash during `update_nonces_from_block`).  The condition `not A or not B` = `not (A and B)` — it skips recovery if **both** indexes have at least one key, even if individual keys are wrong.

- **Recommendation:** Always run `rebuild_state_from_chain()` at startup — it's idempotent, O(n), and the chain is small in this PoC.  Remove the heuristic entirely.

---

### LOW

#### L1 — `config` captured as closure in `create_health_app`; tight coupling

- **File:** `pilar2/nct/nct.py:370-481`
- **Fragment:**
  ```python
  def create_health_app(state: NCTState, redis_client: Any) -> FastAPI:
      # ... endpoints reference `config` from the enclosing main() scope ...
      if not config.authority_pubkey:     # line 440
  ```
- **Risk:** `config` is not a parameter of `create_health_app` — it's captured from `main()`'s local scope.  If `create_health_app` is ever called from another context (e.g. tests, a different entry point), it would fail with `NameError`.  This also makes the function's dependencies implicit.

- **Recommendation:** Pass `config` as an explicit parameter to `create_health_app(state, redis_client, config)`.

---

#### L2 — Constant 100ms polling when idle; no backoff

- **File:** `pilar2/nct/nct.py:359-360`
- **Fragment:**
  ```python
  if not had_work:
      time.sleep(0.1)                  # 10 polls/sec even when chain is idle
  ```
- **Risk:** The result_loop polls RabbitMQ at 10 Hz even when no mining is happening.  At 10 polls/sec × 2 queues (results + registry) = 20 `basic_get` calls per second per NCT instance.  With multiple NCT instances (if scaled), this creates constant load on RabbitMQ.  For a PoC with 1 NCT this is negligible.

- **Recommendation:** Use exponential backoff (0.1 → 0.2 → 0.5 → 1.0s, resetting on work) or switch to `basic_consume` with a callback for lower overhead.

---

#### L3 — `block_loop` logs error but doesn't recover when genesis is missing

- **File:** `pilar2/nct/nct.py:278-280`
- **Fragment:**
  ```python
  if latest is None:
      logger.error("Chain is empty (no genesis block). Run init first.")
      time.sleep(2)
      continue
  ```
- **Risk:** If Redis is wiped mid-run (e.g., `FLUSHALL`), `get_latest_block()` returns `None` and the block_loop spins forever logging errors every 2 seconds.  It never calls `ensure_genesis()` to recreate the genesis block.  Manual intervention required.

- **Recommendation:** Call `ensure_genesis(redis_client)` inside this error path, or at least escalate (exit, alert).

---

#### L4 — `state.chain_height` read without lock; documented as best-effort but misleading

- **File:** `pilar2/nct/state.py:58` (field), `pilar2/nct/nct.py:218` (write), `pilar2/nct/nct.py:386` (read)
- **Risk:** `chain_height` is written in `handle_result` without holding `self.lock` (line 218), and read in the `/status` handler without any synchronization (line 386).  The docstring at state.py:58 acknowledges this ("may be read without lock—best-effort").  In CPython, `int` assignment is atomic, so this won't crash, but the value could be stale by one block.  Low impact (cosmetic only) but contradicts the "thread-safe shared state" promise of the class.

- **Recommendation:** Either (a) protect `chain_height` with a lock, or (b) rename to `_chain_height_best_effort` and add `@property` with a docstring explaining the relaxed semantics.

---

### INFO

#### I1 — Two-phase nonce validation correctly prevents replay

- **Files:** `pilar2/nct/nct.py:429-436` (POST check), `pilar2/nct/nct.py:135-146` (assembly check)
- **Details:** Nonce is validated at POST time against Redis (optimistic), then re-validated at assembly time using `nonce_overlay` (definitive).  The overlay tracks per-sender nonces within the current block, preventing double-inclusion of the same nonce even if two concurrent POSTs passed the optimistic check.  This is the correct pattern (similar to Ethereum's pending nonce).

---

#### I2 — Overlay-based double-spend prevention within a block is correct

- **File:** `pilar2/nct/nct.py:126-166`
- **Details:** The `overlay` dict tracks per-pubkey balance deltas within the current block assembly.  EARN adds to the receiver's overlay; SPEND checks `confirmed_balance + overlay_delta >= amount`.  This prevents a student from spending coins they just earned in the same block, and prevents double-spend of the same balance within a block.  Correct implementation.

---

#### I3 — `shutdown` Event propagation correctly reaches all loops

- **File:** `pilar2/nct/nct.py:567-579`
- **Details:** SIGINT/SIGTERM handlers call `state.shutdown.set()`, which is checked by `block_loop` (line 270) and `result_loop` (line 332).  The main thread polls `shutdown.is_set()` at 1 Hz.  After all loops exit (or daemon threads are killed), the process terminates.  The propagation path is correct for the two worker threads.

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 2 | C1 — Shared `BlockingChannel` across threads, C2 — `ValueError` crash from P1 |
| HIGH | 4 | H1 — `auto_ack=True` loses messages, H2 — Daemon threads no cleanup, H3 — `chain_height` stale on restart, H4 — No error recovery in result_loop |
| MEDIUM | 5 | M1 — Three PoW implementations, M2 — Discarded txns silent, M3 — No amount ceiling, M4 — `/chain` no pagination, M5 — Fragile recovery heuristic |
| LOW | 4 | L1 — Closure capture of config, L2 — Constant polling, L3 — No genesis auto-recovery mid-run, L4 — `chain_height` best-effort semantics |
| INFO | 3 | I1 — Two-phase nonce correct, I2 — Overlay double-spend prevention correct, I3 — Shutdown propagation correct |

**Most impactful fix:** C1 (shared channel across threads) — this is a deterministic concurrency bug that will corrupt the AMQP channel under any sustained load.  Fixing it requires creating separate channels for publish and consume (a 2-line change in `main()`).
