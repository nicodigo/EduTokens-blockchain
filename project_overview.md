# Project Overview — Tp-integrador-SDyPP-CompuMundo

> **Course:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
> **Deadline:** 23/06/2026  
> **Subject:** Distributed Blockchain + CUDA Mining  

---

## What This Project Is

An end-to-end prototype of a **distributed, Proof-of-Work blockchain** built from scratch. The system chains financial transactions (sender → receiver, amount) into blocks whose mining is offloaded to a GPU cluster via CUDA. The project is organized into three sequential pillars:

| Pilar | Topic | Status |
|---|---|---|
| **Pilar 1** | CUDA GPU miner (MD5 PoW) | ✅ Complete |
| **Pilar 2** | Distributed Python microservices + Docker | ✅ Complete |
| **Pilar 3** | Kubernetes (GKE) + CI/CD + Cloud deployment | ✅ Complete |

---

## Repository Layout

```
Tp-integrador-SDyPP-CompuMundo/
├── ASSIGNMENT.md               # Full course assignment specification (Spanish)
├── README.md                   # Minimal stub (just the repo title)
├── project_overview.md         # This file
│
├── pilar1/                     # CUDA GPU miner programs
│   ├── README.md               # Pilar 1 report (hits 2–7 + CPU vs GPU benchmarks)
│   ├── hello_cuda/
│   │   ├── hello.cu            # Hello World kernel (Hit 2)
│   │   └── Makefile
│   ├── thrust/
│   │   ├── thrust_vectors.cu   # Sort 32M ints with Thrust (Hit 3)
│   │   └── Makefile
│   ├── md5_one_input/
│   │   ├── md5_cuda.cu         # Hash a single string on GPU (Hit 4)
│   │   ├── md5.cuh             # Device-side MD5 implementation
│   │   └── Makefile
│   ├── md5_bruteforce/
│   │   ├── md5_bruteforce.cu   # Brute-force nonce search, full space (Hit 5/6)
│   │   ├── md5.cuh
│   │   └── Makefile
│   ├── md5_bf_range/
│   │   ├── md5_range.cu        # Brute-force with [min, max] range (Hit 7)
│   │   ├── md5.cuh
│   │   └── Makefile
│   └── md5_cpu/
│       └── md5_cpu.py          # Python CPU reference implementation
│
└── pilar2/                     # Distributed blockchain infrastructure
    ├── README.md               # Pilar 2 report (design decisions per step)
    ├── docker-compose.yml      # Full stack: Redis, RabbitMQ, NCT, Pool, 2 Workers
    │
    ├── shared/                 # Shared domain models (imported by all services)
    │   ├── block.py            # Transaction + Block dataclasses
    │   ├── miner.py            # MinerService (subprocess wrapper for CUDA binary)
    │   ├── schemas.py          # Pydantic models for HTTP API
    │   └── __init__.py
    │
    ├── broker/                 # RabbitMQ topology + message types
    │   ├── broker.py           # declare_topology(), publish_*, consume_*, broadcast_abort()
    │   ├── messages.py         # TaskMessage, ResultMessage, ControlMessage dataclasses
    │   └── __init__.py
    │
    ├── storage/                # Redis persistence layer
    │   ├── chain_store.py      # save_block(), get_block(), validate_chain()
    │   └── __init__.py
    │
    ├── nct/                    # Node Coordinator (orchestrator)
    │   ├── nct.py              # Main service: 3 threads (block_loop, result_loop, health_loop)
    │   ├── state.py            # NCTState + NCTConfig dataclasses
    │   ├── Dockerfile
    │   └── __init__.py
    │
    ├── pool/                   # Pool Coordinator (partitions work for its workers)
    │   ├── pool.py             # PoolCoordinator: receives task, splits nonce space, collects results
    │   ├── Dockerfile
    │   └── __init__.py
    │
    ├── worker/                 # Mining Worker
    │   ├── worker.py           # Consumes tasks, calls MinerService, publishes results + heartbeats
    │   ├── Dockerfile
    │   └── __init__.py
    │
    ├── miner/                  # Standalone miner module (mirrors shared/miner.py)
    │   ├── miner.py
    │   └── __init__.py
    │
    └── tests/                  # Unit tests (61 tests, all without real infra)
        ├── test_block.py
        ├── test_broker.py
        ├── test_chain_store.py
        ├── test_health.py
        ├── test_miner.py
        ├── test_nct.py
        ├── test_worker.py
        └── __init__.py
```

---

## Pilar 1 — CUDA Miner Deep Dive

### What it does

Implements a GPU-accelerated MD5 hash brute-forcer to solve Proof-of-Work puzzles. Given a `base_string` and a `target_prefix`, it finds a `nonce` such that:

```
MD5(base_string + str(nonce)).startswith(target_prefix)
```

### Key files

| File | Role |
|---|---|
| `md5.cuh` | Device-side MD5. All functions marked `__device__`. Implements RFC 1321 padding + four-round transform. |
| `md5_cuda.cu` | Single-thread kernel: hash one input, verify correctness. |
| `md5_bruteforce.cu` | 1280 blocks × 256 threads = 327,680 concurrent threads. Grid-stride loop. Atomic flag for first-winner termination. |
| `md5_range.cu` | Extends bruteforce with `[range_min, range_max]` bounds. Used by Pilar 2 workers. |
| `md5_cpu.py` | Python `hashlib.md5` sequential reference. Used for CPU vs GPU comparison. |

### Parallelization strategy

```
GPU Thread Grid (327,680 threads)
├── Thread 0  → nonces: 0, 327680, 655360, ...
├── Thread 1  → nonces: 1, 327681, 655361, ...
│   ...
└── Thread N  → nonces: N, N+327680, N+655360, ...
```

First thread to match calls `atomicExch(found_flag, 1)` and writes its result. All other threads check the flag at the start of each iteration and exit early.

### Benchmark results (Google Colab T4 GPU)

| Prefix zeros | CPU time | GPU time | Speedup |
|---|---|---|---|
| 4 | 0.049s | 0.404s | — (CUDA init overhead dominates) |
| 6 | 22.8s | 0.497s | ~45x |
| 7 | 624s | 1.709s | ~365x |

GPU throughput: ~1.1 billion hashes/sec. CPU: ~800K hashes/sec.

### Development environment

- **Platform:** Google Colab (Tesla T4, sm_75, CUDA 12.8, driver 580)
- **Local GPU:** NVIDIA GTX 1060 (sm_61) — incompatible with modern CUDA toolkit
- **Compiler flag:** `nvcc -arch=sm_75`
- **AI assistant used:** DeepSeek

---

## Pilar 2 — Distributed Infrastructure Deep Dive

### Architecture overview

```
                    ┌──────────────────────────────────────────┐
                    │              RabbitMQ (topic exchange)    │
                    │         exchange: "blockchain"            │
                    │                                           │
  POST /transaction │  task.mining ──▶ pool-a.inbox            │
  ───────────────▶  │  result.*    ◀── pool-a.result.*         │
       NCT          │  worker.*    ◀── worker heartbeats        │
       (:8080)      │  control     ──▶ all workers (abort)      │
                    └──────────────────────────────────────────┘
                              │                  ▲
                   publishes  │ task.mining       │ result.pool-a
                              ▼                  │
                         ┌─────────┐             │
                         │ Pool-A  │─────────────┘
                         │ (:8090) │
                         └────┬────┘
                    partition │ nonce space into 2 sub-ranges
                    ┌─────────┴──────────┐
                    ▼                    ▼
             ┌──────────┐        ┌──────────┐
             │ worker-a1│        │ worker-a2│
             │  (:8081) │        │  (:8082) │
             └────┬─────┘        └────┬─────┘
                  │ subprocess         │ subprocess
                  ▼                    ▼
             md5_range (CUDA)    md5_range (CUDA)

                    Redis (:6379)
                    blockchain:blocks → [block0, block1, ...]
```

### Message types (`broker/messages.py`)

```python
TaskMessage    # NCT → workers: fingerprint, difficulty, range_min, range_max
ResultMessage  # worker → NCT: nonce, hash (MD5), worker_id
ControlMessage # NCT → all workers broadcast: action="abort", task_id
```

### NCT — Node Coordinator (`nct/nct.py`)

The brain of the system. Runs 3 threads:

| Thread | Responsibility |
|---|---|
| `block_loop` | Waits for N transactions → creates block → publishes mining task → waits for `block_mined` event → expands nonce space on timeout |
| `result_loop` | Polls `mining_results` queue → verifies PoW (MD5 + prefix check) → persists to Redis → broadcasts abort → signals `block_mined` |
| `health_loop` | Serves FastAPI on `:8080`: `GET /health`, `GET /status`, `POST /transaction` |

**PoW verification (double-check):**
```python
pow_hash = MD5(fingerprint + str(nonce))
valid = (pow_hash == claimed_hash) and pow_hash.startswith("0" * difficulty)
```

**Stale result filter:** if `result.block_index != current_block.index`, the result is silently dropped (another worker already won).

**Timeout expansion:** if no result in `BLOCK_TIMEOUT` seconds, the nonce space doubles and a new task is published.

### Block data model (`shared/block.py`)

```
Transaction
├── sender_pubkey    (str)      Ed25519 public key (64 hex chars)
├── receiver_pubkey  (str)      Ed25519 public key (64 hex chars)
├── amount           (float)    amount being transferred
├── tx_type          (str)      "EARN" (university → student) or "SPEND" (student → vendor)
├── concept          (str)      free-text (e.g. "TP1", "COMEDOR")
├── signature        (str)      Ed25519 signature (128 hex chars) over tx_id
└── timestamp        (float)    unix UTC
   └── tx_id         (SHA-256)  content identifier — signature excluded

Block
├── index           (int)      position in chain
├── timestamp       (float)    unix UTC
├── transactions    (list)     list of Transaction objects (each Ed25519-signed)
├── previous_hash   (str)      SHA-256 of previous block (64 hex chars)
├── difficulty      (int)      number of leading zero nibbles for PoW
├── nonce           (int)      solution found by miner
└── hash            (str)      SHA-256 of complete block (post-mining)

Block.fingerprint   → SHA-256(block WITHOUT nonce)  ← sent to miners
Block.compute_hash()→ SHA-256(block WITH nonce)     ← used for chain linking

Balance index (Redis)
├── balance:{pubkey} → float   per-student balance, updated atomically via pipeline
└── rebuilt from chain on startup if missing (crash recovery)
```

**Two distinct hash algorithms in use:**

| Hash | Algorithm | Purpose |
|---|---|---|
| `tx_id` | SHA-256 | Deterministic transaction ID (signature excluded — computable before signing) |
| `fingerprint` | SHA-256 | Stable block identifier WITHOUT nonce — sent to miners as PoW base string |
| PoW hash | MD5 | Must start with N zeros (cheaper, good enough for demo) |
| `block.hash` | SHA-256 | Final block ID, stored in Redis, used as `previous_hash` for the next block |

### Transaction validation (`shared/block.py` + `nct/nct.py`)

Every transaction is validated in three layers at `POST /transaction`:

1. **Structural** — pubkey lengths (64 hex), signature length (128 hex), amount > 0, tx_type ∈ {EARN, SPEND}, concept non-empty, sender ≠ receiver
2. **Ed25519 signature** — `verify(sender_pubkey, tx_id, signature)` using the `cryptography` library; only the key holder can authorise their own spends
3. **Authority gate** — `EARN` transactions are only accepted from the configured `AUTHORITY_PUBKEY` (the university). `SPEND` transactions have no authority restriction — any student can spend their own balance.

Balance validation for `SPEND` happens at **block assembly time** via `drain_pool_validated()`, which maintains an in-memory overlay of per-student deltas during the block to prevent double-spend within a single block.

### RabbitMQ topology (`broker/broker.py`)

```
Exchange: "blockchain" (topic, durable)

Queues (fixed):
  mining_results   ← bind: result.*     (worker/pool → NCT, single queue shared by all)
  worker_registry  ← bind: worker.*     (heartbeats: worker_id, timestamp)
  {anon per worker}← bind: control      (abort broadcast, exclusive, auto-delete)

Queues (dynamic — per pool):
  pool.{id}.inbox  ← bind: task.mining  (fanout: every pool gets a copy of NCT's task)
  pool.{id}.tasks  ← bind: pool.{id}.task.* (pool's own task queue for workers)
  pool.{id}.results← bind: pool.{id}.result.* (results from pool's workers)
```

NCT publishes ONE message to `task.mining`. Every pool that has bound a queue to that key gets a copy. Each pool then partitions the full nonce space among its own workers. Pools compete with each other; the first valid result wins.

### Redis persistence (`storage/chain_store.py`)

```
Key: blockchain:blocks
Type: Redis List
Values: JSON-serialized Block objects (sort_keys=True for determinism)

Operations:
  RPUSH  → save_block()        append to chain
  LINDEX → get_block(index)    random access by position
  LLEN   → get_chain_height()
  LLEN+LINDEX → get_latest_block()
  full scan → validate_chain() verifies hash chaining integrity

Balance keys:
  GET balance:{pubkey}         → get_balance(pubkey)
  INCRBYFLOAT (pipeline)       → update_balances_from_block()  — atomic per-block
```

AOF persistence enabled (`--appendonly yes`) so chain survives container restarts.

### Worker (`worker/worker.py`)

- Consumes `TaskMessage` from its pool's task queue (or `mining_tasks` if solo)
- Converts `difficulty: int` → `target_prefix: str` (`"0" * difficulty`)
- Calls `MinerService.mine(fingerprint, target_prefix, range_min, range_max)`
- If aborted mid-flight: discards result, acks message
- If solution found: publishes `ResultMessage` to `result.{worker_id}`
- Sends heartbeats every `HEARTBEAT_INTERVAL` seconds to `worker_registry`

### MinerService (`shared/miner.py`)

Thin subprocess wrapper around the CUDA binary:
```python
# The binary_path is split with shlex.split() so compound commands work:
#   binary_path = "python3 /app/miner/cpu_miner.py"  ← CPU fallback in Docker
#   binary_path = "./md5_range"                       ← compiled CUDA binary

result = MinerService(binary_path=binary_path).mine(
    base_string=fingerprint,
    target_prefix="0000",
    range_min=0,
    range_max=1_000_000_000
)
# → MinerResult(nonce=10941, hash="0000b8d7...") | None
```

Parses stdout, handles timeouts and crashes. The binary is compiled from `pilar1/md5_bf_range/`.
In Docker Compose, workers use the **CPU fallback miner** (`python3 /app/miner/cpu_miner.py`)
so the stack runs without a GPU. The CUDA binary can be swapped in on GPU-equipped hosts.

### Docker Compose services

| Service | Image | Port | Depends on |
|---|---|---|---|
| `redis` | redis:7-alpine | 6379 | — |
| `rabbitmq` | rabbitmq:3-management-alpine | 5672, 15672 | — |
| `nct` | custom (python:3.12-alpine) | 8080 | redis (healthy), rabbitmq (healthy) |
| `pool-a` | custom (python:3.12-alpine) | 8090 | rabbitmq (healthy) |
| `worker-a1` | custom (python:3.12-alpine) | 8081 | rabbitmq (healthy) |
| `worker-a2` | custom (python:3.12-alpine) | 8082 | rabbitmq (healthy) |

Workers use **CPU fallback miner** by default: `MINER_BINARY=python3 /app/miner/cpu_miner.py`.
Replace with `./md5_range` (compiled CUDA binary) on GPU hosts.

### Environment variables (key ones)

| Service | Variable | Default | Meaning |
|---|---|---|---|
| NCT | `BLOCK_SIZE` | 5 | Transactions per block |
| NCT | `BLOCK_TIMEOUT` | 30 | Seconds to wait before expanding nonce space |
| NCT | `DIFFICULTY` | 4 | Leading zeros required |
| NCT | `NONCE_SPACE` | 1,000,000,000 | Initial nonce search range |
| NCT | `AUTHORITY_PUBKEY` | — | Ed25519 pubkey authorised to issue EARN transactions |
| NCT | `PORT` | 8080 | HTTP port |
| Pool | `POOL_ID` | autogen | Pool identifier |
| Pool | `POOL_WORKER_COUNT` | 2 | Fallback worker count when no heartbeats seen |
| Worker | `MINER_BINARY` | `./md5_range` | Path to compiled CUDA binary |
| Worker | `POOL_ID` | — | If set, worker joins a pool instead of solo mode |
| Worker | `HEARTBEAT_INTERVAL` | 5 | Seconds between heartbeats |
| Worker | `HEALTH_PORT` | 8081 | HTTP port |
| All | `LOG_FILE` | — | If set, logs go to file + stdout |

### NCT HTTP API (`nct/nct.py` — FastAPI + uvicorn)

| Method | Route | Response |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/status` | `{"chain_height": N, "pending_transactions": M, "current_block": X, "active_pools": P}` |
| `POST` | `/transaction` | `{"tx_id": "..."}` (201) or `{"error": "..."}` (400) |
| `GET` | `/balance/{pubkey}` | `{"address": "...", "balance": 42.5}` |
| `GET` | `/chain` | Full serialised chain as JSON array (audit trail) |

`POST /transaction` validates: structural checks → Ed25519 signature verification → authority gate for EARN.
The server is built with **FastAPI** and served via **uvicorn** in a background thread.

### Test coverage (`tests/`)

226 unit tests, all run without real Redis or RabbitMQ (mocked via `MagicMock` / `FakeClient`):

| Test file | What it covers |
|---|---|
| `test_block.py` | Transaction/Block creation, serialization roundtrip, structural validation, PoW verification |
| `test_broker.py` | Topology declaration, task partitioning, result polling, abort broadcast |
| `test_chain_store.py` | Redis list operations, chain validation, broken-chain detection |
| `test_crypto.py` | Ed25519 signature verification, pubkey-to-address derivation |
| `test_health.py` | HTTP endpoints: `/health`, `/status`, 404 handling |
| `test_miner.py` | Subprocess stdout parsing, timeout, crash, argument passing |
| `test_nct.py` | `verify_pow_result`, `accumulate_transactions`, `handle_result`, `NCTState`, `drain_pool_validated` |
| `test_pool.py` | Pool coordinator, task fanout, result verification, dynamic worker count |
| `test_worker.py` | Heartbeat registration, active worker counting, expiration |

Run all tests:
```bash
cd pilar2 && python -m unittest discover tests/ -v
```

---

## Pilar 3 — Cloud Deployment (GKE + CI/CD)

Full deployment guide: **[pilar3/README.md](pilar3/README.md)**

### Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  GKE Cluster — us-central1-a (zonal, free tier, 2 × e2-standard-2)   │
│                                                                       │
│  ┌── namespace: infra ───────────────────────────────────────────┐   │
│  │  Redis (StatefulSet ×1)     RabbitMQ (StatefulSet ×1)          │   │
│  │  PVC 10Gi, AOF              AMQP :5672 / AMQPS :5671 (TLS)     │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── namespace: blockchain ───────────────────────────────────────┐   │
│  │  NCT (Deployment ×1)        Pool-A (Deployment ×1)              │   │
│  │  ClusterIP :8080            ClusterIP :8090                     │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── namespace: apps ────────────────────────────────────────────┐   │
│  │  nginx-ingress + cert-manager (Let's Encrypt production)        │   │
│  │  Dominio: edutokens.xyz                                         │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── cluster externo (profesor): namespace g-compumundo ─────────┐   │
│  │  Worker GPU (RTX 4060, sm_89, CUDA 12.2)                       │   │
│  │  → AMQPS a rabbitmq.edutokens.xyz:5671                         │   │
│  └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

### Infrastructure as Code (OpenTofu)

10 archivos `.tf` en `pilar3/tofu/`. Administran:
- GKE cluster zonal + node pool (2 × e2-standard-2)
- VPC, subred, Cloud NAT
- Artifact Registry (`edutokens-repo`)
- 2 IPs estáticas PREMIUM (RabbitMQ LoadBalancer, nginx-ingress)
- Workload Identity: GKE pods (SA `gke-pull-images`) y GitHub Actions OIDC (SA `github-actions`)
- IAM bindings para ambos service accounts

### Kubernetes Manifests

16 archivos YAML en 4 namespaces (`infra`, `blockchain`, `apps`, `workers`):
- **Redis** StatefulSet con PVC 10Gi (AOF persistence)
- **RabbitMQ** StatefulSet con TLS (AMQPS para workers externos) + LoadBalancer
- **NCT** Deployment singleton (coordinador), ClusterIP :8080
- **Pool-A** Deployment, ClusterIP :8090
- **nginx-ingress** + **cert-manager** con Let's Encrypt production (wildcard `*.edutokens.xyz`)
- **Worker GPU** deployment para cluster del profesor (`nvidia.com/gpu: 1`)

### Docker Images

| Imagen | Dockerfile | Registro |
|---|---|---|
| `nct:latest` | `pilar2/nct/Dockerfile` | `us-central1-docker.pkg.dev/edutokens-2026/edutokens-repo` |
| `pool:latest` | `pilar2/pool/Dockerfile` | ↑ |
| `worker-cpu:latest` | `pilar2/worker/Dockerfile` | ↑ |
| `worker-gpu:latest` | `pilar3/docker/worker-gpu.Dockerfile` | ↑ |

El worker GPU usa `nvidia/cuda:12.2.2-runtime-ubuntu22.04`, compilado para RTX 4060 (sm_89).

### CI/CD (GitHub Actions)

| Workflow | Trigger | Acción |
|---|---|---|
| `gitleaks.yml` | push + PR a `main` | Escanea historial completo en busca de secretos |
| `ci.yml` | push a `main` | Build + push de las 4 imágenes a Artifact Registry |
| `ci.yml` | PR a `main` | Solo build (verifica compilación, sin pushear) |

La autenticación a GCP usa **Workload Identity Federation** vía OIDC (`token.actions.githubusercontent.com`). Cero service account keys — GitHub emite un token OIDC por workflow run, GCP lo valida contra el pool `github-actions-oidc` y lo mapea a la SA `github-actions`. Docker builds usan GitHub Actions cache para builds incrementales (solo se reconstruyen las capas modificadas).

### Bugs corregidos en producción

Durante el despliegue se identificaron y corrigieron 5 bugs críticos (detallados en `pilar3/.artifacts/handoff-2026-06-20-v2.md`):

| Bug | Fix | Archivo |
|---|---|---|
| NCT `StreamLostError` (RabbitMQ idle timeout) | try/except + `_ensure_rabbitmq_alive()` con reconexión | `pilar2/nct/nct.py` |
| Worker `MinerError: empty stdout` → requeue infinito | stderr a ERROR + dead-letter en vez de requeue | `pilar2/miner/miner.py`, `pilar2/worker/worker.py` |
| Pool `PRECONDITION_FAILED` loop infinito | `auto_ack=True` → `False` en results consumer | `pilar2/pool/pool.py` |
| `send_test_tx.py earn` nonce=0 siempre | Agregar query `/account/{pubkey}` para nonce | `pilar2/tools/send_test_tx.py` |
| `GET /account/{pubkey}` 500 | Eliminar `.decode()` redundante (Redis `decode_responses=True`) | `pilar2/storage/chain_store.py` |

### Decisiones de diseño cloud

| # | Decisión | Fundamento |
|---|---|---|
| D1 | Mismo repo para infra y código | Consigna del TP |
| D2 | OpenTofu solo GCP, kubectl para K8s | Separación limpia, sin chicken-and-egg |
| D3 | AMQPS con certbot wildcard (Let's Encrypt) | Workers validan contra trust store del sistema |
| D4 | LoadBalancer solo en RabbitMQ | Único servicio expuesto externamente |
| D5 | NCT singleton (replicas: 1) | Coordinador único por diseño |
| D6 | Workload Identity (GKE + GitHub OIDC) | Cero service account keys |
| D7 | Redis StatefulSet con PVC + AOF | Persistencia de la cadena |
| D8 | Sin NetworkPolicy en producción | Rompieron DNS y cert-manager en testing |
| D9 | Secretos `.example` + gitignore | Templates versionados, valores nunca commiteados |

---

## How to Run Locally (Pilar 2)

```bash
# Prerequisites: Docker + Docker Compose installed
# (CUDA binary optional — CPU fallback miner works out of the box)

# Build the CUDA miner first (requires NVIDIA GPU + CUDA toolkit, or Colab)
cd pilar1/md5_bf_range && make
cp md5_range ../../pilar2/

# Start all services
cd pilar2
docker compose up --build -d

# Submit a transaction
curl -X POST http://localhost:8080/transaction \
  -H "Content-Type: application/json" \
  -d '{"sender": "alice", "receiver": "bob", "amount": 10.0}'

# Check chain status
curl http://localhost:8080/status

# RabbitMQ Management UI
open http://localhost:15672  # guest / guest
```

---

## Key Design Decisions

1. **MD5 for PoW, SHA-256 for chain linking** — MD5 is fast on GPU (good for demo), SHA-256 is collision-resistant (good for tamper evidence).

2. **Ed25519 digital signatures** — Every transaction is signed by its sender using Ed25519 (via the `cryptography` library). The NCT verifies the signature at `POST /transaction` time. This provides non-repudiation: only the key holder can authorise a spend. The signing happens client-side (frontend, admin scripts); the blockchain never sees private keys.

3. **Authority model** — `EARN` transactions (university → student) are gated: only the configured `AUTHORITY_PUBKEY` can issue them. `SPEND` transactions (student → vendor) require a valid Ed25519 signature from the student. Balance validation happens at block assembly time to allow fast async POSTs.

4. **`subprocess` for CUDA** — Python calls the compiled binary via `subprocess.run()` instead of PyCUDA. Keeps Pilar 1 (C++/CUDA) and Pilar 2 (Python) cleanly separated. A CPU fallback miner (`cpu_miner.py`) ships in Docker so the stack works without a GPU.

5. **Threading over asyncio** — NCT uses `threading` because the bottleneck is network I/O (RabbitMQ, Redis), not CPU. Three daemon threads share state via `NCTState` with a `threading.Lock`.

6. **FastAPI + uvicorn** — HTTP layer uses FastAPI with Pydantic schemas for strong request/response contracts, served by uvicorn in a background thread.

7. **Competitive pool architecture (audit H3)** — NCT publishes one task (full range) per block to `task.mining` via a topic exchange. Pools subscribe via topic fanout and ALL compete on the SAME nonce space; the first valid result wins. With N pools, (N-1)/N of GPU compute is redundant work — an accepted trade-off in this PoC for implementation simplicity. The `active_pools` field on the NCT `/status` endpoint makes this redundancy observable. For production, the architecture would be refactored to partition the nonce space across pools cooperatively.

8. **Lazy imports in broker** — `pika` is only imported when a connection is actually needed, so the test suite runs without RabbitMQ installed.

9. **Deterministic serialization** — All JSON is serialized with `sort_keys=True` to ensure consistent SHA-256 hashes across Python versions and platforms.

---

## Tech Stack Summary

| Layer | Technology |
|---|---|
| GPU Mining | CUDA C++ (nvcc), MD5 custom implementation |
| CPU Mining | Python 3.11+ hashlib |
| GPU Parallelism | NVIDIA Thrust (CCCL), raw CUDA kernels |
| Services | Python 3.12, FastAPI + uvicorn |
| Message Queue | RabbitMQ 3 (pika client), topic exchange |
| Storage | Redis 7 (redis-py client), AOF persistence |
| Signatures | Ed25519 (cryptography library) |
| Containerization | Docker + Docker Compose |
| Testing | Python `unittest` + `MagicMock` |
| HTTP API | FastAPI + Pydantic (strong request/response validation) |
| Cloud | Google Kubernetes Engine (GKE) via OpenTofu |
| CI/CD | GitHub Actions (gitleaks, Docker build) via Workload Identity Federation |
| Secret scanning | gitleaks v8 (standalone binary, no license required) |
| Container registry | Artifact Registry (Docker) |
| TLS | cert-manager + Let's Encrypt (production wildcard) |
| Ingress | nginx-ingress |
| GitOps | kubectl (manifiestos declarativos en `pilar3/k8s/`) |