# Audit P4 — Mining Pipeline (Miner + Worker + Pool)

**Scope:** `pilar2/miner/miner.py` (135 lines), `pilar2/miner/cpu_miner.py` (46 lines), `pilar2/worker/worker.py` (351 lines), `pilar2/pool/pool.py` (441 lines)

**Audit date:** 2026-06-18

---

## Findings

### CRITICAL

#### C1 — Worker abort mechanism cannot interrupt a running mining task

- **File:** `pilar2/worker/worker.py:239-296` (task consumer + callback), `pilar2/worker/worker.py:212-233` (control listener + callback)
- **Fragment:**
  ```python
  # Both callbacks are registered on the SAME channel:
  self._channel.basic_consume(queue=queue_name, on_message_callback=self._on_task, ...)      # line 241
  self._channel.basic_consume(queue=anonymous,  on_message_callback=self._on_control, ...)    # line 223

  def _on_task(self, ch, method, _properties, body):
      self._aborted.clear()
      result = self.miner.mine(...)    # ← BLOCKS the I/O thread (subprocess.run, up to 300s)
      if self._aborted.is_set():       # ← NEVER True because _on_control was never called
          ...

  def _on_control(self, _ch, _method, _properties, body):
      msg = ControlMessage.from_json(body.decode())
      if msg.action == "abort" and msg.task_id == self._current_task_id:
          self._aborted.set()           # ← NEVER executes while _on_task is blocked in mine()
  ```
- **Risk:** pika's `BlockingConnection.start_consuming()` dispatches all callbacks on a **single I/O thread**.  When `_on_task` calls `self.miner.mine()` — a blocking `subprocess.run()` — the I/O thread is blocked for the entire mining duration (up to 300 seconds).  During this time, `basic_consume` cannot dispatch any other callback, including `_on_control`.  The abort signal sits in RabbitMQ's TCP buffer, unread.

  **Result:** the abort mechanism is **completely ineffective**.  Workers always finish their current mining call before checking `self._aborted.is_set()`, and since `_on_control` was never called, the event is never set.  The worker processes the full nonce range, then acks and moves on — having wasted all that computation after a solution was already found elsewhere.

  This wastes significant resources: for `difficulty=4` and `nonce_space=1e9`, mining takes ~10 minutes on CPU.  If worker A finds the solution at minute 1, workers B and C continue mining for 9 more minutes, burning CPU for nothing.

- **Recommendation:** Either (a) run the control listener in a **separate thread** with its own `basic_consume` loop (not `start_consuming()`), (b) use `subprocess.Popen` + polling loop that checks `self._aborted` and calls `proc.kill()`, or (c) switch to `pika.SelectConnection` with async I/O where callbacks don't block.  Simplest fix for the PoC:

  ```python
  # In _on_task, replace blocking subprocess.run with polling:
  proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
  while proc.poll() is None:
      if self._aborted.is_set():
          proc.kill()
          proc.wait()
          ch.basic_ack(delivery_tag=method.delivery_tag)
          return
      time.sleep(0.1)
  ```

---

### HIGH

#### H1 — `MinerError` / `PermissionError` in `_on_task` crashes pika consumer; task lost

- **File:** `pilar2/worker/worker.py:260-265` (_on_task), `pilar2/miner/miner.py:115-130` (mine)
- **Fragment:**
  ```python
  # worker.py:260 — no try/except around mine()
  result = self.miner.mine(
      base_string=task.fingerprint,
      target_prefix=target_prefix,
      range_min=task.range_min,
      range_max=task.range_max,
  )

  # miner.py:124 — PermissionError NOT caught
  except FileNotFoundError as exc:          # line 124
      raise MinerError(...) from exc
  # PermissionError is a subclass of OSError, NOT FileNotFoundError
  ```
- **Risk:** Two failure modes crash the worker's pika consumer:
  1. `subprocess.run` raises `PermissionError` when the binary exists but is not executable.  `miner.py` only catches `FileNotFoundError` (line 124), so `PermissionError` propagates unhandled into `_on_task`.
  2. `miner.py` may raise `MinerError` for other failures (timeout, non-0/1 exit code, unparseable output).  `_on_task` has **no try/except** around the `mine()` call.

  In either case, the exception propagates out of `_on_task`.  pika's callback exception handling closes the channel (or crashes `start_consuming()`).  The task message (`auto_ack=False`) is **lost** — RabbitMQ requeues it, but the closed channel can't process it.  The worker is dead until restart.

- **Recommendation:** Wrap `self.miner.mine()` in `_on_task` with a `try/except Exception` that logs the error and calls `ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)` to requeue the task for another worker.  Also fix `miner.py` to catch `PermissionError`.

---

#### H2 — Pool worker-result callback `auto_ack=True` + no error handling → lost PoW

- **File:** `pilar2/pool/pool.py:157-158,265-300`
- **Fragment:**
  ```python
  self._channel.basic_consume(
      queue=results_q,
      on_message_callback=self._on_worker_result,
      auto_ack=True,            # ← auto_ack BEFORE processing
  )

  def _on_worker_result(self, _ch, _method, _props, body):
      result = ResultMessage.from_json(body.decode())  # ← JSON parse can raise
      # … verify PoW, forward to NCT …
      # Any exception → message already acked → LOST
  ```
- **Risk:** The pool acknowledges worker results before verifying them.  If `ResultMessage.from_json()` raises (malformed body), or the PoW verification raises (unexpected data), or the forward publish raises (connection error) — the message is already acknowledged and **gone**.  A valid PoW solution found by a worker that spent CPU cycles is discarded without being forwarded to the NCT.  The NCT times out and re-publishes the mining task, wasting more compute.

- **Recommendation:** Change to `auto_ack=False` and add `ch.basic_ack(delivery_tag=method.delivery_tag)` only after successful forward to NCT.  Wrap the callback body in `try/except` to nack on failure.

---

#### H3 — Worker task connection can silently die while heartbeat connection stays alive

- **File:** `pilar2/worker/worker.py:117-118` (task channel), `pilar2/worker/worker.py:197-198` (heartbeat channel)
- **Fragment:**
  ```python
  conn = get_connection(url=self.rmq_url)
  self._channel = conn.channel()           # ← task/control channel (one connection)

  # … separate heartbeat connection …
  hb_conn = get_connection(url=self.rmq_url)
  hb_channel = hb_conn.channel()            # ← heartbeat channel (another connection)
  ```
- **Risk:** The worker uses **two independent connections** — one for tasks + control, one for heartbeats.  If the task connection drops (RabbitMQ restart, network hiccup), `start_consuming()` on `self._channel` raises `StreamLostError` — the worker crashes.  But the heartbeat connection is separate; it might still be alive (heartbeats keep flowing).  The pool sees heartbeats → thinks the worker is alive → assigns tasks to a dead worker → tasks time out.

  Conversely, if the heartbeat connection drops, heartbeats stop → pool expires the worker → worker stops receiving tasks even though its task channel is fine.  The worker is alive but idle, and nobody knows.

- **Recommendation:** Use a **single connection** with multiple channels (pika supports this).  If the connection dies, both heartbeats and tasks fail simultaneously → the problem is detected uniformly.  Or implement connection-loss detection and reconnect on both connections.

---

#### H4 — Worker heartbeats fail silently after first error; no reconnection attempt

- **File:** `pilar2/worker/worker.py:195-206`
- **Fragment:**
  ```python
  def _heartbeat_loop(self) -> None:
      hb_conn = get_connection(url=self.rmq_url)
      hb_channel = hb_conn.channel()
      while not self._shutdown.is_set():
          self._shutdown.wait(timeout=self.heartbeat_interval)
          if not self._shutdown.is_set():
              try:
                  self._send_heartbeat(hb_channel)
              except Exception:
                  logger.warning("Heartbeat send failed (connection may be down)")
                  # ← Loop continues but hb_channel is dead — ALL future sends fail too
  ```
- **Risk:** Once `_send_heartbeat` fails (e.g., connection dropped), the exception is caught and logged, but the loop continues with the **same dead channel**.  All subsequent heartbeats fail silently.  After `heartbeat_timeout` seconds (15s default), the pool removes the worker from its active set.  The worker is alive and processing tasks, but the pool stops assigning new tasks to it — the worker becomes idle and contributes no mining power.

- **Recommendation:** On heartbeat failure, attempt reconnection:
  ```python
  except Exception:
      logger.warning("Heartbeat failed — reconnecting...")
      try:
          hb_conn = get_connection(url=self.rmq_url)
          hb_channel = hb_conn.channel()
      except Exception:
          time.sleep(5)
  ```

---

### MEDIUM

#### M1 — Dead-worker monitor re-publishes with overlapping ranges; waste but not harmful

- **File:** `pilar2/pool/pool.py:318-370`
- **Fragment:**
  ```python
  def _monitor_loop(self):
      while self._monitor_active.is_set():
          # …
          if active < self._original_worker_count:
              publish_tasks(                          # line 348
                  …, num_workers=count,
                  range_size=self._current_nonce_space,   # ← FULL range, not just orphans
              )
              self._original_worker_count = count
  ```
- **Risk:** When a worker dies, the monitor re-publishes the **full nonce space** (not just the orphaned sub-range) to the remaining workers.  Workers that are still mining their original sub-range will receive a new, overlapping task.  Since `basic_qos(prefetch_count=1)` limits each worker to one unacknowledged task, the new task waits in the queue until the worker finishes its current one.  This causes:
  - Workers may mine overlapping ranges (inefficient but not incorrect — results are still valid).
  - On each re-publish, the `_original_worker_count` drops → if another worker dies, the range is re-published again → cascading re-publishes under churn.
  - The monitor doesn't send abort to surviving workers for the original task.

  Not a correctness issue (the NCT accepts the first valid result and broadcasts abort), but wastes compute.

- **Recommendation:** Track which sub-ranges were assigned to which workers.  On worker death, only re-publish the orphaned sub-range.  Alternatively, send abort to surviving workers and re-publish the full range cleanly.

---

#### M2 — Pool monitor thread race condition on task transition

- **File:** `pilar2/pool/pool.py:253-257` (monitor creation), `pilar2/pool/pool.py:294-300` (result handler cleanup)
- **Fragment:**
  ```python
  # _on_mining_task:
  self._monitor_active.set()
  if self._monitor_thread is None or not self._monitor_thread.is_alive():
      self._monitor_thread = threading.Thread(target=self._monitor_loop, ...)
      self._monitor_thread.start()
  # … starts mining, sets self._current_block_index ← NO LOCK

  # _on_worker_result:
  self._monitor_active.clear()                     # ← stops monitor
  self._current_block_index = None                  # ← NO LOCK
  ```
- **Risk:** After `_on_worker_result` clears `_monitor_active` and sets `_current_block_index=None`, the old monitor thread may still execute one more iteration (the check at line 328 happens before the signal is acknowledged).  Meanwhile, `_on_mining_task` for the NEXT block sets `_current_block_index` to the new block's index.  The old monitor thread sees `_current_block_index` is NOT None (it's the new block's index!) and continues monitoring — but it's monitoring with the **old** `_monitor_active` semantics.  Two monitor threads could run concurrently for the same block.

  In practice, the `_monitor_active.wait(timeout=5)` ensures the old thread checks the event within 5 seconds, and the window for this race is narrow.  Low probability but a data race exists.

- **Recommendation:** Protect `_current_block_index` and `_current_fingerprint` with a lock.  Have the monitor thread also verify `_current_task_id` matches what it was monitoring.

---

#### M3 — `cpu_miner.py` has no built-in timeout; relies entirely on wrapper

- **File:** `pilar2/miner/cpu_miner.py:31-34`
- **Fragment:**
  ```python
  for nonce in range(range_min, range_max + 1):   # ← no time bound
      digest = hashlib.md5(...).hexdigest()
      if digest.startswith(target_prefix):
          ...
  ```
- **Risk:** The CPU miner iterates sequentially over the full nonce range with no time limit.  It relies on `MinerService.mine()` applying a 300s `subprocess.run(timeout=...)`.  If the wrapper's timeout fails (e.g., `subprocess.run` timeout doesn't work on all platforms, or the binary is called directly without the wrapper), the process runs indefinitely.  For `difficulty=5` and `range_size=1e9`, this could take hours on CPU.

- **Recommendation:** Add a `time.time()` check inside the loop, or accept `timeout` as a CLI argument.  Minimum: document that the process is unbounded without the wrapper.

---

#### M4 — Miner `PermissionError` not caught — crashes worker (see H1)

- **File:** `pilar2/miner/miner.py:120-126`
- **Details:** Only `FileNotFoundError` and `subprocess.TimeoutExpired` are caught.  `PermissionError` (binary not executable) propagates.

- **Recommendation:** Catch `OSError` (parent of both) or all exceptions with a generic error message.

---

#### M5 — Worker `_current_task_id` and `tasks_processed` racing with health endpoint

- **File:** `pilar2/worker/worker.py:67,255,295`
- **Fragment:**
  ```python
  # _on_task (pika I/O thread):
  self._current_task_id = task.task_id              # line 255 — write
  self.tasks_processed += 1                         # line 295 — read-modify-write

  # /status endpoint (uvicorn thread):
  current_task=worker._current_task_id,             # line 67 — read (no lock)
  tasks_processed=worker.tasks_processed,           # line 68 — read (no lock)
  ```
- **Risk:** `_current_task_id` is a `str | None` — in CPython, assigning a string is atomic (single pointer write), so the health endpoint will see either the old or new value.  Safe.  But `tasks_processed += 1` is a **compound operation** (load, add, store) that can interleave with a read.  The health endpoint could read a partially updated value, though in practice CPython's GIL makes this unlikely to produce garbage — at worst, the displayed count is 1 behind.

- **Recommendation:** Low-severity for a PoC counter.  Use `threading.Lock` or `itertools.count` (thread-safe in CPython) if precise reporting matters.

---

#### M6 — `_broadcast_abort` called by both NCT and Pool; double abort

- **File:** `pilar2/pool/pool.py:294,305-312` (pool), `pilar2/broker/broker.py:279-286` (NCT)
- **Risk:** When a valid result is found, both the pool (`_broadcast_abort` at line 294) and the NCT (`broadcast_abort` in `handle_result` at nct.py:221) send abort signals.  Workers receive two abort messages for the same task.  The worker's `_on_control` processes both — the second is a no-op (task_id won't match since `_current_task_id` has moved on, or if it hasn't, `_aborted` is already set).  Harmless but wastes network.

- **Recommendation:** Remove one of the two.  The NCT's global abort is sufficient since it covers all pools and solo workers.

---

### LOW

#### L1 — Miner stderr discarded for successful runs; debug info lost

- **File:** `pilar2/miner/miner.py:127-130`
- **Fragment:**
  ```python
  if proc.returncode not in (0, 1):
      raise MinerError(f"Miner exited with code {proc.returncode}:\n{proc.stderr}")
  # stderr for returncode 0 or 1 is silently discarded
  ```
- **Risk:** If the CUDA miner outputs warnings to stderr (e.g., "GPU thermal throttling", "low memory"), these are silently lost.  Debugging performance issues requires modifying the code.  For the CPU fallback, stderr is never used anyway.

- **Recommendation:** Log `proc.stderr` at DEBUG level when non-empty, regardless of return code.

---

#### L2 — No process group for miner subprocess; orphans on kill

- **File:** `pilar2/miner/miner.py:115-119`
- **Risk:** If the worker process is killed (SIGKILL) while `subprocess.run` is executing, the `md5_range` child process becomes orphaned and continues running.  With `difficulty=4` and a large range, this consumes CPU until completion or manual kill.  The `timeout` mechanism in `subprocess.run` sends SIGTERM on timeout, but SIGKILL to the parent doesn't propagate.

- **Recommendation:** Use `subprocess.Popen` with `start_new_session=True` (or `preexec_fn=os.setpgrp`) to create a process group, and kill the group on shutdown.  Or rely on Docker's `--init` to reap orphans.

---

#### L3 — CPU miner has no buffered output; partial reads possible

- **File:** `pilar2/miner/cpu_miner.py:37-38`
- **Details:** `print()` with default buffering (line-buffered when piped) could produce partial output if the subprocess is killed mid-line.  The Miner's regex parser (`_OUTPUT_RE`) would return no match → `MinerError("Could not parse miner output")` → raised in worker → crashes consumer (see H1).  Very unlikely in practice (the two `print` calls happen in microseconds).

- **Recommendation:** Flush stdout after printing: `sys.stdout.flush()`.

---

### INFO

#### I1 — Worker correctly uses separate channel for heartbeats

- **File:** `pilar2/worker/worker.py:196-198`
- **Details:** Unlike the NCT (Audit P3-C1), the worker creates a dedicated connection and channel for heartbeats (`hb_conn`, `hb_channel`), separate from the task/control channel.  This correctly avoids channel sharing across threads.  Good pattern.

---

#### I2 — Pool `basic_qos(prefetch_count=1)` prevents worker overload

- **File:** `pilar2/pool/pool.py:154`
- **Details:** `prefetch_count=1` ensures each worker receives at most one unacknowledged task at a time.  Workers process tasks sequentially.  This prevents a fast worker from accumulating a backlog while a slow worker starves.  Correct configuration.

---

#### I3 — `shlex.split` correctly handles multi-token `MINER_BINARY`

- **File:** `pilar2/miner/miner.py:113`
- **Details:** `shlex.split(self.binary_path)` correctly splits `"python3 /app/miner/cpu_miner.py"` into `["python3", "/app/miner/cpu_miner.py"]`.  Without `shlex`, passing the raw string to `subprocess.run` would fail (it expects a list or a single command string with `shell=True`).  Good design choice.

---

#### I4 — Pool correctly partitions nonce space evenly among workers

- **File:** `pilar2/pool/pool.py:240-248` → `broker/broker.py:102-142`
- **Details:** `publish_tasks` divides the nonce space into `num_workers` equal chunks, with the last worker getting the remainder.  For 2 workers and `range_size=1e9`: worker 0 gets [0, 499,999,999], worker 1 gets [500,000,000, 999,999,999].  Correct integer partitioning with no off-by-one errors on the last chunk (line 121: `range_size - 1`). ✓

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 1 | C1 — Abort mechanism completely ineffective (I/O thread blocked) |
| HIGH | 4 | H1 — `MinerError`/`PermissionError` crashes consumer, H2 — Pool `auto_ack` loses PoW results, H3 — Split connections out of sync, H4 — Heartbeat failure cascade |
| MEDIUM | 6 | M1 — Overlapping range re-publish, M2 — Monitor thread race, M3 — CPU miner no timeout, M4 — `PermissionError` uncaught, M5 — Health data race, M6 — Double abort broadcast |
| LOW | 3 | L1 — stderr discarded, L2 — Orphaned subprocess, L3 — Unflushed output |
| INFO | 4 | I1 — Separate heartbeat channel, I2 — QoS correct, I3 — shlex correct, I4 — Range partitioning correct |

**Most impactful fix:** C1 — the abort mechanism is the system's primary way to stop wasted computation after a solution is found.  It doesn't work at all.  Every worker processes its full nonce range even after the block is already mined.  With 2 workers and a pool, this means at least 50% of CPU time is wasted.
