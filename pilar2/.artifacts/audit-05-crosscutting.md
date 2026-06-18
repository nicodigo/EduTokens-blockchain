# Audit P5 — Cross-Cutting Concerns

**Scope:** Thread safety (all components), error handling patterns, security model, Docker/infrastructure, logging/observability, test coverage gap analysis

**Audit date:** 2026-06-18

**Note:** This phase synthesises cross-component patterns. Findings already documented in P1–P4 are cross-referenced, not duplicated.

---

## Findings

### HIGH

#### H1 — No `.dockerignore`; bloated build context shipped to every container

- **Files:** `pilar2/nct/Dockerfile`, `pilar2/worker/Dockerfile`, `pilar2/pool/Dockerfile`
- **Fragment (all Dockerfiles):**
  ```dockerfile
  COPY shared/ shared/
  COPY broker/ broker/
  COPY storage/ storage/   # NCT only
  COPY nct/ nct/           # NCT only
  # … etc
  ```
- **Risk:** Without a `.dockerignore`, `docker build` sends the **entire project directory** as build context, including:
  - `.artifacts/` (audit reports, growing)
  - `tests/` (10 test files + fixtures)
  - `__pycache__/` directories
  - `.git/` directory (if built from the repo root)
  - `*.md` documentation files
  - `pilar1/` (CUDA code, irrelevant to Pilar 2 services)

  Each Dockerfile only `COPY`s specific subdirectories, but the build context is still uploaded in full.  On slow connections or large repos, this adds seconds-to-minutes per build.  More critically, the `.git` directory contains the full commit history — building from a dirty working tree could leak uncommitted secrets.

- **Recommendation:** Add a `.dockerignore` at the project root:
  ```
  .git
  .artifacts
  tests
  __pycache__
  *.pyc
  *.md
  pilar1
  .env
  ```

---

#### H2 — No health checks on application containers; Docker can't detect app-level failures

- **File:** `pilar2/docker-compose.yml:31-45` (nct), `pilar2/docker-compose.yml:47-59` (pool-a), etc.
- **Fragment:**
  ```yaml
  nct:
    build: …
    ports: ["8080:8080"]
    depends_on:
      redis:    { condition: service_healthy }
      rabbitmq: { condition: service_healthy }
    # ← No healthcheck defined for nct itself
  ```
- **Risk:** Redis and RabbitMQ have health checks (lines 9-12, 20-23), but the application containers (nct, pool, worker) do **not**.  Docker relies on the process staying alive — but as documented in P3-H4, a daemon thread crash (result_loop) leaves the process alive while the blockchain is frozen.  Docker would report the container as "healthy" while the app is non-functional.

  Without health checks, Docker Compose cannot restart unhealthy containers automatically (requires `restart: unless-stopped` + health check failing).

- **Recommendation:** Add health checks to all three services:
  ```yaml
  nct:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 10s
      timeout: 3s
      retries: 3
  ```
  Note: the NCT Dockerfile already installs `curl` (line 3), making this zero-cost.  The worker and pool Dockerfiles would need `curl` added via `apk add --no-cache curl`.

---

#### H3 — All three `uvicorn.run()` calls suppress HTTP access logs

- **File:** `pilar2/nct/nct.py:487`, `pilar2/worker/worker.py:176`, `pilar2/pool/pool.py:390`
- **Fragment:**
  ```python
  uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
  ```
- **Risk:** `log_level="warning"` suppresses uvicorn's default `info`-level output, which includes HTTP access logs (method, path, status code, response time).  Without access logs:
  - No audit trail of who called `/transaction`, `/balance`, `/chain`.
  - Impossible to detect abuse (flooding, scraping) without external tooling.
  - Debugging latency issues requires instrumenting the code manually.
  - The `/chain` endpoint could be called repeatedly by an attacker with no trace.

  The `--access-log` flag is implicitly disabled because the log level is `warning`.

- **Recommendation:** Set `log_level="info"` and redirect uvicorn access logs to the application logger, or keep `warning` for uvicorn's internal logs but explicitly pass `access_log=True` to retain the HTTP audit trail.

---

#### H4 — No rate limiting or request size limits on POST `/transaction`; trivial DoS

- **File:** `pilar2/nct/nct.py:391-456`
- **Risk:** The `/transaction` endpoint has **no rate limiting, no request body size limit, and no connection limit**.  An attacker can:
  - Flood the endpoint with valid-structure but invalid-signature transactions → CPU exhaustion from Ed25519 verification.
  - Send oversized JSON bodies → memory exhaustion.
  - Open thousands of connections → file descriptor exhaustion.

  The Pydantic `TransactionRequest` validates structure but runs BEFORE signature verification — so an attacker can craft structurally valid but unsigned transactions and force 64 Ed25519 verifications per request (sender + receiver pubkey parsing).  Actually, it's one Ed25519 verify per request.  Still, at 10K requests/sec, this saturates CPU.

- **Recommendation:** Add:
  - FastAPI middleware for rate limiting (e.g. `slowapi` with Redis backend).
  - `Request` body size limit (`uvicorn.run(..., limit_max_requests=1000)` or FastAPI `max_request_size`).
  - Connection limit at the uvicorn level (`limit_concurrency`).

---

### MEDIUM

#### M1 — `AUTHORITY_PUBKEY` empty by default; system silently rejects all EARN transactions

- **File:** `pilar2/docker-compose.yml:38`
- **Fragment:**
  ```yaml
  environment:
    - AUTHORITY_PUBKEY=
  ```
- **Risk:** The default configuration has an empty authority public key.  The NCT starts successfully, but every `POST /transaction` with `tx_type=EARN` returns a 400 error: *"EARN transactions require AUTHORITY_PUBKEY to be configured"*.  There is **no startup warning** that the authority is unconfigured.  A first-time user running `docker-compose up` would see all services healthy but be unable to issue EARN transactions, with no clear indication why.

- **Recommendation:** Add a startup log at `WARNING` level when `AUTHORITY_PUBKEY` is empty.  Better: refuse to start the NCT if `AUTHORITY_PUBKEY` is not set (since the system's core function is broken without it).

---

#### M2 — Environment variable parsing crashes with opaque `ValueError` on malformed input

- **File:** `pilar2/nct/nct.py:68` (`_env_int`), `pilar2/pool/pool.py:420` (`int(os.getenv(...))`), `pilar2/worker/worker.py:327` (`float(os.getenv(...))`)
- **Fragment:**
  ```python
  def _env_int(name: str, default: int) -> int:
      return int(os.getenv(name, str(default)))   # ← ValueError if non-numeric
  ```
- **Risk:** If any numeric environment variable is set to a non-numeric value (e.g. `BLOCK_SIZE=cinco`, `DIFFICULTY=high`), `int()` raises `ValueError` with a raw Python traceback.  In Docker, the container crashes at startup with an unhelpful error message.  There's no validation that `BLOCK_SIZE > 0`, `DIFFICULTY >= 0`, `NONCE_SPACE > 0`, etc.  The pool and worker have the same pattern but without even a helper function — direct `int(os.getenv(...))` calls.

- **Recommendation:** Add input validation with friendly error messages:
  ```python
  def _env_int(name: str, default: int, min_val: int = 0) -> int:
      raw = os.getenv(name, str(default))
      try:
          val = int(raw)
      except ValueError:
          raise SystemExit(f"Invalid {name}={raw!r} — must be an integer")
      if val < min_val:
          raise SystemExit(f"Invalid {name}={val} — must be >= {min_val}")
      return val
  ```

---

#### M3 — `.env` not loaded by docker-compose; all env vars hardcoded in YAML

- **File:** `pilar2/docker-compose.yml:31-91`
- **Risk:** All environment variables are hardcoded in the Compose file.  There is no `env_file:` directive pointing to a `.env` file.  To change configuration (e.g. set `AUTHORITY_PUBKEY`), users must edit the Compose file directly — risking accidental commits of secrets.  The `.env` file is gitignored (`code/uni/EduTokens/EduTokens-blockchain/.gitignore:42`), but it's never read by Compose.

- **Recommendation:** Add `env_file: .env` to each service and document the expected variables.  Keep hardcoded defaults in the Compose file as fallback only.  This separates configuration from infrastructure definition.

---

#### M4 — No test coverage for integration scenarios, failure modes, or abort mechanism

- **Files:** `pilar2/tests/*.py` (10 test files)
- **Coverage gaps identified:**
  | Scenario | Covered? | Why it matters |
  |----------|----------|----------------|
  | Unit: Transaction validation | ✅ test_block.py | |
  | Unit: Ed25519 verify | ✅ test_crypto.py | |
  | Unit: Block validation | ✅ test_block.py | |
  | Unit: Chain store CRUD | ✅ test_chain_store.py | |
  | Unit: Broker functions (dead code) | ✅ test_broker.py | See P2-M1 |
  | Unit: Miner parsing | ✅ test_miner.py | |
  | Unit: Pool partitioning | ✅ test_pool.py | |
  | Unit: Worker task processing | ✅ test_worker.py | |
  | Unit: NCT endpoints | ✅ test_nct.py | |
  | **Integration: full mining flow** | ❌ | Core value prop untested end-to-end |
  | **Failure: Redis down mid-mining** | ❌ | P3-H4 crash untested |
  | **Failure: RabbitMQ restart** | ❌ | P2-H2 reconnect untested |
  | **Failure: Worker crash mid-mining** | ❌ | P4-M1 monitor recovery untested |
  | **Abort mechanism effectiveness** | ❌ | P4-C1 bug undetected by tests |
  | **Timeout expansion loop** | ❌ | P3 block_loop retry untested |
  | **Nonce race (two concurrent POSTs)** | ❌ | P3-I1 correctness untested |
  | **Chain corruption recovery** | ❌ | P2-M3 validate_chain untested |

- **Risk:** The 10 test files provide good unit coverage, but the most critical behaviors (mining end-to-end, failure recovery, abort) have **zero automated test coverage**.  The P4-C1 abort bug — the most impactful finding in the entire audit — went undetected because the abort mechanism was never tested in an integrated scenario.

- **Recommendation:** Add at minimum: (a) an integration test that spawns NCT + pool + 2 workers (mock RabbitMQ/Redis with Docker or `pytest-rabbitmq`/`fakeredis`), submits a transaction, and verifies a block is mined; (b) a test that verifies the abort signal reaches workers before mining completes (will fail until P4-C1 is fixed).

---

#### M5 — `rabbitmq:3-management-alpine` exposes management UI on 15672 with default credentials

- **File:** `pilar2/docker-compose.yml:18-19`
- **Fragment:**
  ```yaml
  rabbitmq:
    image: rabbitmq:3-management-alpine
    ports:
      - "5672:5672"
      - "15672:15672"         # ← Management UI exposed on all interfaces
  ```
- **Risk:** The RabbitMQ management plugin is enabled and its web UI is exposed on port 15672, bound to `0.0.0.0` (default).  The default credentials (`guest:guest`) allow remote management access on the host network.  Anyone who can reach port 15672 can:
  - View all queues, messages, and routing topology
  - Purge queues (delete in-flight mining tasks)
  - Create/delete exchanges and bindings
  - Monitor message rates (information disclosure)

  For a PoC on a local machine this is acceptable.  For any deployment accessible beyond `localhost`, this is a critical security issue.

- **Recommendation:** (a) Bind 15672 to `127.0.0.1` only: `"127.0.0.1:15672:15672"`, or (b) disable the management plugin in production builds, or (c) set `RABBITMQ_DEFAULT_USER`/`RABBITMQ_DEFAULT_PASS` environment variables.

---

### LOW

#### L1 — NCT Dockerfile installs `curl` but never uses it (until health checks are added)

- **File:** `pilar2/nct/Dockerfile:4`
- **Fragment:**
  ```dockerfile
  RUN apk add --no-cache curl
  ```
- **Risk:** Dead dependency — `curl` is installed but never invoked.  Wastes ~2 MB of image size and adds a dependency that could have CVEs.  (If H2 is addressed and curl is used for health checks, this becomes justified.)

- **Recommendation:** Remove `curl` from the NCT Dockerfile, or keep it and implement H2 (health checks using curl).

---

#### L2 — No `restart` policy on application containers

- **File:** `pilar2/docker-compose.yml:31-91`
- **Risk:** If the NCT, pool, or worker containers crash (e.g., due to an unhandled exception), Docker does **not** restart them automatically.  The blockchain stops until manual intervention.  Combined with H2 (no health checks), Docker can't even detect application-level crashes.  Process-level crashes (exit code ≠ 0) would stop the container but without `restart: unless-stopped`, it stays dead.

- **Recommendation:** Add `restart: unless-stopped` to nct, pool-a, worker-a1, and worker-a2 services.

---

#### L3 — No structured logging; all logs are plain text

- **Files:** All `.py` files using `logger.info/warning/error`
- **Risk:** Logs are plain text with Python's default `logging.Formatter`.  They contain no machine-parseable fields (JSON, key=value pairs).  In a multi-container environment, correlating logs across NCT, pool, and workers requires manual timestamp matching.  This is acceptable for a PoC but makes operational debugging significantly harder.

- **Recommendation:** Use `python-json-logger` or a custom formatter to emit JSON logs with `service`, `thread`, and `trace_id` fields.  Or use Docker's `journald` log driver with `--log-opt tag=...` to label logs by container.

---

#### L4 — `worker-a2` container maps internal port 8081 to host 8082; confusing for local debugging

- **File:** `pilar2/docker-compose.yml:90`
- **Fragment:**
  ```yaml
  worker-a2:
    ports:
      - "8082:8081"       # ← External 8082 → internal 8081
    environment:
      - HEALTH_PORT=8081  # ← Internal port is still 8081
  ```
- **Risk:** This is **intentionally correct** — Docker remaps the internal port.  However, a developer running the worker locally (without Docker) would see both workers trying to bind to port 8081, causing a port conflict.  The Compose configuration masks this; the code has no `--port` override capability beyond the `HEALTH_PORT` env var.

- **Recommendation:** Document that `HEALTH_PORT` must be unique when running multiple workers outside Docker.  Or auto-detect port conflicts and increment.

---

### INFO

#### I1 — Global thread safety matrix

| Component | Threads | Shared mutable state | Protection mechanism | Issues |
|-----------|---------|---------------------|---------------------|--------|
| **NCT** | 3 (block, result, health) | `NCTState.tx_pool` | `tx_lock` | ✓ |
| | | `NCTState._current_block` | `lock` | ✓ |
| | | `NCTState._workers` | `_worker_lock` | ✓ |
| | | `NCTState.chain_height` | **None** | P3-L4 |
| | | `channel` (pika) | **None** (shared) | **P3-C1** |
| **Worker** | 3 (I/O, health, heartbeat) | `_current_task_id` | **None** | P4-M5 |
| | | `tasks_processed` | **None** | P4-M5 |
| | | `_aborted` | `threading.Event` | ✓ |
| | | `_channel` (main + hb) | Separate connections | ✓ (unlike NCT) |
| **Pool** | 2 (I/O, monitor) | `_worker_heartbeats` | `_heartbeat_lock` | ✓ |
| | | `_current_block_index` | **None** | P4-M2 |
| | | `_current_fingerprint` | **None** | P4-M2 |

---

#### I2 — Error handling coverage map

| Component | Critical path | Has try/except? | On failure… |
|-----------|--------------|-----------------|-------------|
| NCT | `result_loop` polling | ❌ None | Thread crashes silently | P3-H4 |
| NCT | `handle_result` persist | ❌ None | Exception propagates to loop | P3-H4 |
| NCT | `POST /transaction` | ✅ crypto_verify `except InvalidSignature` | Returns 400 | But misses `ValueError` — P1-C1 |
| NCT | `block_loop` publish | ❌ None | Thread crashes | |
| Worker | `_on_task` mining | ❌ None | Crashes consumer | P4-H1 |
| Worker | `_heartbeat_loop` send | ✅ `except Exception: pass` | Heartbeats stop forever | P4-H4 |
| Worker | `_on_control` parse | ❌ None | Auto_ack ⇒ message lost | |
| Pool | `_on_worker_result` verify | ❌ None | Auto_ack ⇒ result lost | P4-H2 |
| Pool | `_on_mining_task` process | ❌ None | Explicit ack not sent ⇒ requeued | Somewhat safe |
| Miner | `mine()` subprocess | ✅ `TimeoutExpired`, `FileNotFoundError` | Raises `MinerError` | Misses `PermissionError` — P4-M4 |

**Pattern:** The codebase has **no consistent error handling strategy**.  Some paths catch exceptions, others silently crash.  RabbitMQ message acknowledgment is a critical boundary — 4 out of 5 `auto_ack=True` consumers lose messages on failure.  The single `auto_ack=False` consumer (worker tasks) is the only safe one.

---

#### I3 — `.gitignore` correctly excludes secrets and build artifacts

- **File:** `code/uni/EduTokens/EduTokens-blockchain/.gitignore`
- **Details:** The root `.gitignore` excludes `.env`, `secrets.json`, `__pycache__/`, `.vscode/`, `.idea/`, `*.pyc`, build directories, and coverage reports.  This is comprehensive and correctly configured.  No action needed.

---

## Summary

| Severity | Count | IDs |
|----------|-------|-----|
| HIGH | 4 | H1 — No `.dockerignore`, H2 — No app health checks, H3 — HTTP access logs suppressed, H4 — No rate limiting on POST |
| MEDIUM | 5 | M1 — `AUTHORITY_PUBKEY` empty silently, M2 — Env var parse crashes, M3 — `.env` not loaded by Compose, M4 — No integration/failure tests, M5 — RabbitMQ management exposed |
| LOW | 4 | L1 — Unused `curl` dependency, L2 — No restart policy, L3 — No structured logging, L4 — Port mapping confusion |
| INFO | 3 | I1 — Thread safety matrix, I2 — Error handling map, I3 — `.gitignore` correct |

**Most impactful fix:** H2 (health checks) — without them, Docker cannot detect when the blockchain is frozen (daemon thread crash) and cannot auto-restart.  Combined with `restart: unless-stopped` (L2), this would give the system self-healing capability.
