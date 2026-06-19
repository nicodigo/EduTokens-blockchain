# Audit P2 — Infrastructure Layer (Broker + Storage)

**Scope:** `pilar2/broker/broker.py` (306 lines), `pilar2/broker/messages.py` (150 lines), `pilar2/storage/chain_store.py` (226 lines)

**Audit date:** 2026-06-18

**Status:** FIXED

---

## Findings

### HIGH

#### H1 — Messages published without persistence; lost on RabbitMQ restart

- **File:** `pilar2/broker/broker.py:132-137,164-168,213-217,268-272`
- **Fragment (example — `publish_mining_task`):**
  ```python
  channel.basic_publish(
      exchange=EXCHANGE,
      routing_key="task.mining",
      body=task.to_json(),
  )
  ```
- **Risk:** Every `basic_publish()` call in the broker omits `properties=pika.BasicProperties(delivery_mode=2)`.  The queues are declared `durable=True`, but messages are transient (default `delivery_mode=1`).  If RabbitMQ restarts — even with durable queues — **all in-flight messages are lost**, including:
  - Mining tasks (`task.mining`) → NCT waits forever (until timeout expansion)
  - Worker results (`result.*`) → block mining silently stalls
  - Abort signals (`control`) → workers keep mining stale tasks
  - Heartbeats (`worker.*`) → pool thinks workers died

  The NCT's `block_timeout` expansion provides a partial safety net for lost mining tasks, but transaction throughput degrades.  Lost results are worse: a valid PoW solution evaporates and must be rediscovered.

- **Recommendation:** Set `delivery_mode=2` on all `basic_publish` calls.  Example:
  ```python
  from pika import BasicProperties
  channel.basic_publish(
      exchange=EXCHANGE,
      routing_key="task.mining",
      body=task.to_json(),
      properties=BasicProperties(delivery_mode=2),
  )
  ```

---

#### H2 — No reconnection logic; any connection drop crashes the service

- **File:** `pilar2/broker/broker.py:34-73` (get_connection), and all consumers: `nct/nct.py:542-543`, `pool/pool.py:123-124`, `worker/worker.py:117-118`
- **Fragment (NCT startup):**
  ```python
  rmq_conn = get_connection(url=config.rabbitmq_url)   # nct.py:542
  channel = rmq_conn.channel()                          # nct.py:543
  # … channel used in 3 daemon threads forever …
  ```
- **Risk:** `get_connection()` retries on `AMQPConnectionError` during initial connect — good.  But once connected, the returned `BlockingConnection` has **zero** automatic recovery.  If RabbitMQ restarts, or a network partition occurs, or the TCP connection times out:
  - The next `basic_get`, `basic_publish`, or `basic_consume` on the stale channel raises `pika.exceptions.StreamLostError` or `ConnectionClosed`.
  - In the NCT, this exception propagates unhandled out of `result_loop` or `block_loop`, crashing that thread silently (daemon thread → process stays alive but non-functional).
  - The worker's `start_consuming()` loop similarly crashes.
  - The health HTTP server stays up (`/health` returns 200) — **the system appears healthy while the blockchain is frozen.**

  No component implements `pika`'s `ReconnectionStrategy` or even a `try/except` + reconnect loop around the main consume loop.

- **Recommendation:** Either (a) wrap the main consume loop in a `while not shutdown: try: … except (ConnectionClosed, StreamLostError): reconnect` block in each service, or (b) switch to `pika.SelectConnection` with the built-in retry adapter, or (c) at minimum, add a health-check that pings RabbitMQ and reports degraded status.

---

#### H3 — `save_block` + `update_balances_from_block` not atomic; crash window leaves inconsistent state

- **File:** `pilar2/nct/nct.py:215-217` (call site), `pilar2/storage/chain_store.py:70-77,176-194` (implementations)
- **Fragment (NCT handle_result):**
  ```python
  save_block(redis_client, current_block)                       # line 215
  update_balances_from_block(redis_client, current_block)       # line 216
  update_nonces_from_block(redis_client, current_block)         # line 217
  ```
- **Risk:** Three separate Redis commands.  If the process crashes or Redis restarts between `save_block` (line 215) and the balance/nonce updates (lines 216-217):
  - The block is persisted in the chain (`blockchain:blocks` list).
  - Balances are stale — `get_balance()` returns pre-block values.
  - Nonces are stale — replay attacks become possible (same nonce accepted again).
  - The `ensure_genesis()` recovery at startup (`nct/nct.py:510-515`) calls `rebuild_state_from_chain()` which fixes this — but only on restart.  Between crash and restart, `/balance/{addr}` and `/account/{pubkey}` return **wrong data**.

  Additionally, a crash *during* `update_balances_from_block` (pipeline execute) could partially update balances.  The pipeline batches commands but is NOT wrapped in `MULTI/EXEC` (the docstring at line 180-181 acknowledges this: "Not transactional across the full block (documented limitation)").

- **Recommendation:** Wrap all three operations in a Redis `MULTI/EXEC` transaction.  If that's not feasible (RPUSH + INCRBYFLOAT on different keys in a transaction is supported by Redis), at minimum: (a) save balances to a temporary key, (b) RPUSH the block, (c) rename temporary balances to real balances — making the update atomic from the reader's perspective.  Alternatively, accept the eventual-consistency model and shorten the recovery window by running `rebuild_state_from_chain` on a timer.

---

### MEDIUM

#### M1 — Four broker functions are dead production code

- **File:** `pilar2/broker/broker.py:184-207` (consume_result), `pilar2/broker/broker.py:226-244` (setup_control_listener), `pilar2/broker/broker.py:247-263` (start_consuming_tasks), `pilar2/broker/broker.py:266-273` (publish_result)
- **Evidence:**
  - `consume_result` — only called by `test_broker.py:140,149`.  The NCT's `result_loop` (`nct/nct.py:336-338`) uses `channel.basic_get` directly.
  - `setup_control_listener` — only called by `test_broker.py:181,195`.  The worker has its own implementation at `worker/worker.py:212` (`_setup_control_listener`).
  - `start_consuming_tasks` — only called by `test_broker.py:231,244`.  The worker uses `_setup_task_consumer` at `worker/worker.py:239`.
  - `publish_result` — only called by `test_broker.py`.  The worker publishes directly at `worker/worker.py:285-289`.
- **Risk:** These functions are exported in `broker/__init__.py` and appear to be part of the public API, but no production code uses them.  They can silently diverge from the actual implementations in the worker/NCT.  For example, `start_consuming_tasks` binds to `TASKS_QUEUE` (the old queue name `"mining_tasks"`), but the pool architecture uses `pool.{id}.tasks` — the function is wired to a queue that no publisher writes to.  A developer who reads the broker module might assume these are the canonical entry points and build on them incorrectly.

- **Recommendation:** Either (a) delete the unused functions, or (b) refactor the worker and NCT to use them (so there's a single source of truth for broker operations), or (c) mark them `_deprecated` with a docstring warning.

---

#### M2 — `publish_tasks` default `routing_key_prefix="task"` matches no consumer

- **File:** `pilar2/broker/broker.py:102-142`
- **Fragment:**
  ```python
  def publish_tasks(
      channel: Any,
      block_index: int,
      fingerprint: str,
      difficulty: int,
      num_workers: int = 3,
      range_size: int = 1_000_000_000,
      routing_key_prefix: str = "task",   # ← dangerous default
  ) -> list[TaskMessage]:
      …
      routing_key=f"{routing_key_prefix}.{i}",
  ```
- **Risk:** The default `routing_key_prefix="task"` produces routing keys `task.0`, `task.1`, etc.  No queue in the system binds to `task.*` — the pool binds to `pool.{pool_id}.task.*` and the worker (solo mode) binds to `task.mining`.  If a caller omits the `routing_key_prefix` argument, messages are published to a routing key with zero consumers → **silently dropped** (no `mandatory` flag, so RabbitMQ doesn't return them).

  Fortunately, the only production caller is the pool (`pool/pool.py:240-248`), which correctly passes `routing_key_prefix=f"pool.{self.pool_id}.task"`.  But the dangerous default is a latent footgun.

- **Recommendation:** Either remove the default value (make `routing_key_prefix` required) or set it to a value that actually routes somewhere (e.g. `"pool.default.task"`) with a docstring warning.  Alternatively, assert that the prefix ends with `.task` to catch misconfiguration early.

---

#### M3 — `validate_chain()` never called at runtime

- **File:** `pilar2/storage/chain_store.py:114-130`
- **Risk:** `validate_chain()` walks the full chain and checks structural integrity (chaining, hash consistency, transaction validation).  It is defined, exported, and tested — but **never called** in the NCT startup, health loop, or any other production path.  Chain corruption (e.g. a bug in serialization, a Redis bit-flip, or a hand-edited entry) would go undetected indefinitely.  The `/chain` endpoint returns blocks without validation.

- **Recommendation:** Call `validate_chain()` in `ensure_genesis()` after `rebuild_state_from_chain`, or in a periodic background thread, or at minimum on the `/chain` endpoint.  Log and expose validation status via `/health` (e.g. `chain_valid: true/false`).

---

#### M4 — `update_balances_from_block` pipeline is batched but not transactional

- **File:** `pilar2/storage/chain_store.py:176-194`
- **Fragment:**
  ```python
  pipe = client.pipeline()         # line 188 — batched, NOT MULTI/EXEC
  for tx in block.transactions:
      if tx.tx_type == "EARN":
          pipe.incrbyfloat(…)
      elif tx.tx_type == "SPEND":
          pipe.incrbyfloat(…)
  pipe.execute()                   # line 194
  ```
- **Risk:** `redis-py`'s `pipeline()` without `transaction=True` (default) sends all commands in a single network round-trip but does **not** guarantee atomicity.  If the connection drops mid-pipeline, some `INCRBYFLOAT` commands execute and others don't — leaving balances partially updated.  Since `save_block()` already committed (line 215 of nct.py) and there's no rollback mechanism, the chain is permanently inconsistent until `rebuild_state_from_chain()` runs at next startup.  The docstring at line 180-181 correctly documents this limitation, but the consequence severity (silent balance corruption) is understated.

- **Recommendation:** Use `client.pipeline(transaction=True)` (MULTI/EXEC) for atomicity.  Note: `INCRBYFLOAT` is supported in MULTI/EXEC blocks in Redis 2.6+.  This would make the balance update all-or-nothing.

---

#### M5 — `_set_nonce` is private but used externally; inconsistent with public API

- **File:** `pilar2/storage/chain_store.py:158-160` (definition), `pilar2/storage/chain_store.py:220` (usage in rebuild_state_from_chain)
- **Fragment:**
  ```python
  def _set_nonce(client: Any, pubkey: str, nonce: int) -> None:   # line 158 — underscore prefix
      """Set the nonce for *pubkey* (used during rebuild)."""
      client.set(f"{NONCE_PREFIX}{pubkey}", nonce)
  ```
- **Risk:** `_set_nonce` uses a leading underscore (Python convention for "private"), but it's called from `rebuild_state_from_chain` (same module, so technically OK) and potentially from external code that reads the module.  More importantly, there's a public `get_nonce()` but no public `set_nonce()` — the only way to set a nonce is via `update_nonces_from_block`.  This asymmetry makes programmatic nonce repair (e.g. an admin tool to fix a stuck account) unnecessarily awkward.

- **Recommendation:** Rename to `set_nonce()` to match `get_nonce()`, or add a docstring explaining why it's private.

---

### LOW

#### L1 — No `mandatory` flag on publishes; messages to dead routing keys silently dropped

- **File:** All `basic_publish` calls in `pilar2/broker/broker.py` (lines 132, 164, 213, 268)
- **Risk:** Without `mandatory=True`, RabbitMQ silently drops messages published to a routing key with no bound queue.  If a configuration error causes a pool to bind to the wrong routing key, or if the pool hasn't started yet, mining tasks vanish without any log or error.  The `Basic.Return` mechanism (which requires `mandatory=True`) would at least log the drop.

- **Recommendation:** Set `mandatory=True` on critical publishes (mining tasks, abort signals) and add a `Basic.Return` callback that logs warnings.  For heartbeats and results, `mandatory` is less critical since these are fire-and-forget by nature.

---

#### L2 — Heartbeat opens a dedicated connection per worker thread; no connection reuse

- **File:** `pilar2/worker/worker.py:196-198`
- **Fragment:**
  ```python
  def _heartbeat_loop(self) -> None:
      hb_conn = get_connection(url=self.rmq_url)    # new connection
      hb_channel = hb_conn.channel()                 # new channel
  ```
- **Risk:** Each worker maintains two RabbitMQ connections (one for tasks, one for heartbeats).  With N workers, this is 2N connections.  For a PoC with 2 workers this is negligible (4 connections), but it scales poorly.  More critically, the heartbeat connection has its own retry logic (via `get_connection`) but if it drops mid-loop, the `except Exception` at line 205 silently swallows the error without attempting reconnection — heartbeats stop permanently.  The pool would then expire the worker after `heartbeat_timeout` seconds (15s default).

- **Recommendation:** Use a single connection with separate channels (pika supports this).  If a dedicated heartbeat connection is kept, add a reconnect loop inside `_heartbeat_loop`.

---

#### L3 — `ensure_genesis` recovery detection is fragile

- **File:** `pilar2/nct/nct.py:509-515`
- **Fragment:**
  ```python
  if get_chain_height(redis_client) > 0:
      balance_keys = redis_client.keys(f"{BALANCE_PREFIX}*")
      nonce_keys = redis_client.keys(f"{NONCE_PREFIX}*")
      if not balance_keys or not nonce_keys:
          logger.warning("Índices de estado vacíos con cadena existente — reconstruyendo")
          rebuild_balances_from_chain(redis_client)
  ```
- **Risk:** The recovery heuristic checks whether *any* balance key and *any* nonce key exist.  If one exists but the other doesn't (e.g. balances were rebuilt but nonces weren't), the condition `not balance_keys or not nonce_keys` is `False`, and recovery is skipped.  More robust: check whether the number of balance keys matches the expected number of unique pubkeys in the chain, or simply always run `rebuild_state_from_chain` at startup (since it's O(n) and the chain is small).

- **Recommendation:** Always run `rebuild_state_from_chain` at startup — it's idempotent and cheap for a PoC chain.  Remove the heuristic.

---

#### L4 — `test_broker.py` tests functions that aren't used in production

- **File:** `pilar2/tests/test_broker.py`
- **Risk:** The test file (`test_broker.py`) thoroughly tests `consume_result`, `setup_control_listener`, `start_consuming_tasks`, and `publish_result` — all dead production code (see M1).  These tests give a false sense of coverage.  Meanwhile, the *actual* broker operations performed by the NCT and worker (direct `basic_get`, pool queue bindings, worker-specific control listeners) are NOT covered by `test_broker.py` — they're only implicitly tested through `test_nct.py` and `test_worker.py` with mocks.

- **Recommendation:** After addressing M1 (removing or refactoring dead code), update `test_broker.py` to cover the actual broker operations used in production.

---

### INFO

#### I1 — AOF persistence configured correctly

- **File:** `pilar2/docker-compose.yml:10`
- **Details:** `redis-server --appendonly yes` is correctly set in the Compose file.  Combined with the named volume (`redis_data`), Redis data survives container restarts.  This is the correct configuration for a PoC blockchain.

---

#### I2 — Crash recovery via `rebuild_state_from_chain` is well-designed

- **File:** `pilar2/storage/chain_store.py:197-222`
- **Details:** The function walks all blocks and recomputes balances and nonces from scratch.  It correctly handles:
  - EARN → credits receiver
  - SPEND → debits sender
  - Nonce = tx.nonce + 1 (last writer wins per sender)
  - Skips `None` blocks (corrupt entries)
  This is the correct idempotent recovery strategy.

---

#### I3 — Queue durability is correctly declared

- **File:** `pilar2/broker/broker.py:86-94`
- **Details:** All queues (`mining_results`, `worker_registry`) are declared `durable=True`.  The exchange is also `durable=True`.  Pool queues (`inbox`, `tasks`, `results`, `registry`) are also durable (`pool/pool.py:131-148`).  Durable queues survive RabbitMQ restarts — only the messages within them are lost (see H1).

---

#### I4 — Lazy imports enable testing without infrastructure

- **File:** `pilar2/broker/broker.py:47-48` (pika), `pilar2/storage/chain_store.py:52-53` (redis)
- **Details:** Both `pika` and `redis` are imported inside functions, not at module level.  This allows the test suite to import these modules without RabbitMQ or Redis installed.  Good design pattern for testability.

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| HIGH | 3 | H1 — No message persistence, H2 — No reconnection logic, H3 — Non-atomic block save + balance update |
| MEDIUM | 5 | M1 — Dead production code (4 broker functions), M2 — Dangerous default routing_key, M3 — validate_chain never called, M4 — Pipeline not transactional, M5 — _set_nonce private vs public API |
| LOW | 4 | L1 — No mandatory flag, L2 — Per-thread connections with no reconnect, L3 — Fragile recovery heuristic, L4 — Tests cover dead code |
| INFO | 4 | I1 — AOF persistence correct, I2 — Crash recovery well-designed, I3 — Queue durability correct, I4 — Lazy imports for testability |

**Most impactful fix:** H2 (no reconnection logic) — a RabbitMQ restart silently freezes the blockchain while `/health` reports 200 OK.  Combined with H1 (lost messages) and H3 (inconsistent state on crash), the system has a brittle failure mode under infrastructure disruption.
