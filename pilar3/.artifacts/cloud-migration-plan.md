# Plan de Migración: Docker Compose → GKE

> **Proyecto:** EduTokens — Blockchain Distribuida y CUDA  
> **Curso:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
> **Última actualización:** 2026-06-20 (sesión 2: fixes de StreamLostError, MinerError, Pool auto_ack, nonce en cmd_earn, .decode en get_discarded_txns)  
> **Deadline entrega:** 2026-06-23

---

## Estado general

| Fase | Estado |
|---|---|
| Fase 1 — Build & Push de imágenes | ✅ completado |
| Fase 2 — Infraestructura (OpenTofu) | ✅ aplicado |
| Fase 3 — Manifiestos Kubernetes | ✅ funcionando |
| Fase 4 — Workers GPU | ✅ desplegado, conectado, minado validado |
| Fase 5 — CI/CD | ⏳ pendiente (GitHub Actions WIP + Workload Identity Pool) |
| Fase 6 — Observabilidad | 📎 diferida |
| Fase 7 — Verificación | 🔶 parcial (bloque 2 creado con 1 tx; timeout de minado por pool caída infinita — necesita nuevo deploy con fixes) |

---

## Arquitectura

```
┌──────────────────────────────────────────────────────────────────────┐
│  GKE Cluster — us-central1-a (zonal, free tier)                      │
│                                                                       │
│  ┌── namespace: infra ───────────────────────────────────────────┐   │
│  │  Redis (StatefulSet ×1)       RabbitMQ (StatefulSet ×1)        │   │
│  │  PVC 10Gi, AOF               PVC 10Gi                          │   │
│  │  ClusterIP :6379             TLS: certbot wildcard (LE)        │   │
│  │                              LoadBalancer IP: 35.255.11.243    │   │
│  │                              :5672 AMQP (interno)              │   │
│  │                              :5671 AMQPS (workers externos)    │   │
│  │                              cacertfile: /etc/ssl/certs/...    │   │
│  │                              initContainer: fix-cookie         │   │
│  │                              management: port-forward only     │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── namespace: blockchain ───────────────────────────────────────┐   │
│  │  NCT (Deployment ×1)          Pool-A (Deployment ×1)            │   │
│  │  ClusterIP :8080             ClusterIP :8090                   │   │
│  │  → amqp://rabbitmq.infra     → amqp://rabbitmq.infra            │   │
│  │  → redis://redis.infra       accedido vía port-forward         │   │
│  │  KSA: blockchain (Workload ID)                                 │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── namespace: apps ────────────────────────────────────────────┐   │
│  │  Ingress (nginx) + cert-manager (Let's Encrypt production)     │   │
│  │  IP: 35.255.210.109                                            │   │
│  │  ─ edutokens.xyz → frontend:80 (futuro)                       │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  Sin NetworkPolicy. Init container en RabbitMQ para erlang cookie.    │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ AMQPS :5671 (TLS, verify_peer)
                                   │ dominio: rabbitmq.edutokens.xyz
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Cluster GPU (profesor) — namespace g-compumundo                      │
│  Worker GPU (Deployment ×1) — NVIDIA RTX 4060 (sm_89), CUDA 12.2     │
│  → rabbitmq.edutokens.xyz:5671 — SSL handshake exitoso               │
│  → certbot wildcard validado contra trust store del sistema           │
│  Bloque 1 minado exitosamente el 2026-06-20.                          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## IPs y dominios

| IP | Recurso | Dominio | Puerto | Origen |
|---|---|---|---|---|
| `35.255.11.243` | RabbitMQ LoadBalancer | `rabbitmq.edutokens.xyz` | 5671 | OpenTofu (PREMIUM) |
| `35.255.210.109` | nginx-ingress | `edutokens.xyz`, `*.edutokens.xyz` | 443 | OpenTofu (PREMIUM) |

**Dominio raíz:** `edutokens.xyz` (comprado, con certbot wildcard `*.edutokens.xyz`).

---

## Flujo de despliegue

```bash
# ── Fase A: Infraestructura GCP (OpenTofu) ──
cd pilar3/tofu
tofu init && tofu plan && tofu apply

# ── Fase B: Cluster (kubectl) ──
gcloud container clusters get-credentials edutokens-cluster --zone us-central1-a --project edutokens-2026
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml

# ── Fase C: Secretos TLS ──
kubectl -n infra create secret generic rabbitmq-amqps-tls \
  --from-file=tls.crt=/path/to/fullchain.pem \
  --from-file=tls.key=/path/to/privkey.pem

# ── Fase D: Secretos de app ──
cp pilar3/k8s/blockchain/secret.yaml.example    pilar3/k8s/blockchain/secret.yaml
cp pilar3/k8s/infra/rabbitmq-secret.yaml.example pilar3/k8s/infra/rabbitmq-secret.yaml
# Completar valores

# ── Fase E: Build, push, deploy ──
./pilar3/scripts/build-push.sh
./pilar3/scripts/deploy-k8s.sh

# ── Fase F: Workers (cluster GPU) ──
cp pilar3/k8s/workers/worker-gpu-secret.yaml.example pilar3/k8s/workers/worker-gpu-secret.yaml
kubectl apply -f pilar3/k8s/workers/
```

---

## Fase 1 — Build & Push de imágenes Docker ✅

| Imagen | Dockerfile | Build context |
|---|---|---|
| `nct:latest` | `pilar2/nct/Dockerfile` | `pilar2/` |
| `pool:latest` | `pilar2/pool/Dockerfile` | `pilar2/` |
| `worker-cpu:latest` | `pilar2/worker/Dockerfile` | `pilar2/` |
| `worker-gpu:latest` | `pilar3/docker/worker-gpu.Dockerfile` | repo root |

Automatizado: `pilar3/scripts/build-push.sh`.

---

## Fase 2 — Infraestructura OpenTofu ✅

**10 archivos .tf activos**. Administra:
- GKE cluster zonal + node pool (2 × e2-standard-2)
- VPC, subred, Cloud NAT
- Artifact Registry
- Workload Identity (SA, IAM bindings, KSA anotación)
- 2 IPs estáticas PREMIUM (rabbitmq, nginx-ingress)
- APIs habilitadas (5)

**Eliminados:** `cert-manager.tf` (puro comentario), `firewall.tf` (GKE auto-crea reglas).

---

## Fase 3 — Manifiestos Kubernetes ✅

**16 archivos YAML** en 4 namespaces:

```
pilar3/k8s/
├── ingress.yaml                    (apps, edutokens.xyz → frontend)
├── cert-manager/cluster-issuer.yaml (Let's Encrypt production)
├── apps/namespace.yaml
├── infra/
│   ├── namespace.yaml
│   ├── redis-*.yaml               (StatefulSet + ClusterIP, AOF)
│   ├── rabbitmq-statefulset.yaml  (TLS, initContainer fix-cookie, fsGroup: 100)
│   ├── rabbitmq-service.yaml      (LoadBalancer + IP Premium)
│   ├── rabbitmq-configmap.yaml    (verify_peer, cacertfile Alpine)
│   └── rabbitmq-secret.yaml.example
├── blockchain/
│   ├── namespace.yaml
│   ├── configmap.yaml | secret.yaml.example | service-account.yaml
│   ├── nct-*.yaml                (Deployment singleton + ClusterIP)
│   └── pool-*.yaml               (Deployment + ClusterIP)
└── workers/
    ├── worker-gpu-deployment.yaml  (nvidia.com/gpu: 1, g-compumundo)
    └── worker-gpu-secret.yaml.example
```

**Scripts:** `build-push.sh`, `deploy-k8s.sh`.

**Correcciones aplicadas durante el despliegue:**

| Problema | Causa | Fix |
|---|---|---|
| Erlang cookie `must be accessible by owner only` | `fsGroup: 100` rompe permisos del cookie en PVC | `RABBITMQ_ERLANG_COOKIE` vía Secret + initContainer que borra cookie viejo |
| RabbitMQ `cacertfile` inexistente | Alpine no tiene trust store implícito | `cacertfile = /etc/ssl/certs/ca-certificates.crt` |
| Let's Encrypt rate limit producción | 5 fallas/hora en debug de NetworkPolicy | Staging para debug, producción para final |
| IP STANDARD no aceptada por GKE LoadBalancer | `network_tier = STANDARD` | Migrado a PREMIUM (por defecto en `google_compute_address`) |
| Ingress no rutea a pods en otros namespaces | Limitación de K8s | Ingress en `apps`, NCT/RabbitMQ vía port-forward |
| Cert-manager self-check timeout | Ingress principal capturaba tráfico del solver ACME | Borrar Ingress temporalmente durante emisión |
| AMQPS `Connection refused` desde worker GPU | IP del Service no asignada por tier mismatch | Nueva IP PREMIUM + forwarding rule + health check |

---

## Fase 4 — Workers GPU ✅

**Cluster del profesor** — namespace `g-compumundo`:
- NVIDIA RTX 4060 (sm_89), CUDA 12.2.2
- Imagen: `worker-gpu:latest` (nvidia/cuda:12.2.2-runtime-ubuntu22.04)
- Binario: `pilar1/md5_range_4060/md5_range`
- Conexión AMQPS con TLS a `rabbitmq.edutokens.xyz:5671`
- SSL handshake validado (certbot wildcard contra trust store Ubuntu)
- 1 worker registrado en pool-a, health en `:8081`
- **Bloque 1 minado exitosamente** (2026-06-20)

**Para reconstruir la imagen** (si cambia el código Python):
```bash
./pilar3/scripts/build-push.sh   # build + push de worker-gpu:latest
kubectl -n g-compumundo rollout restart deployment worker-gpu
```

---

## Fase 5 — CI/CD ⏳

**Pendiente.** Propuesta: `ci.yml` (build + push) + `gitleaks.yml` (scan de secretos).
Workload Identity para GitHub Actions provisionado en `iam.tf`. Falta crear Workload Identity Pool.

---

## Fase 6 — Observabilidad 📎

**Diferida.** Solo si sobra tiempo.

---

## Fase 7 — Verificación 🔶

**Parcial.** Bloque génesis + bloque 1 (minado) + bloque 2 (creado con 1 EARN tx). El bloque 2 no se minó porque la pool entraba en loop infinito de reconexión al recibir resultados de workers.

**Fixes aplicados en esta sesión:**

| Fix | Archivos | Estado |
|---|---|---|
| NCT `StreamLostError` crash al publicar bloque | `nct/nct.py` — try/except con reconexión en `publish_mining_task` + `basic_get` | ✅ Implementado, necesita rebuild + redeploy |
| Worker `MinerError: empty stdout` — requeue infinito | `miner/miner.py` — stderr a ERROR + incluido en error msg; `worker/worker.py` — dead-letter en vez de requeue | ✅ Implementado, necesita rebuild + redeploy |
| Pool `PRECONDITION_FAILED` loop infinito en reconexión | `pool/pool.py` — `auto_ack=True` → `False` en results consumer de reconexión | ✅ Implementado, necesita rebuild + redeploy |
| `send_test_tx.py earn` siempre usaba nonce=0 | `tools/send_test_tx.py` — `cmd_earn` ahora consulta `/account/{pubkey}` para nonce | ✅ Implementado, necesita rebuild |
| `get_discarded_txns` crashea con `decode_responses=True` | `storage/chain_store.py` — eliminar `.decode()` | ✅ Implementado, necesita rebuild |

**Pendiente:**
1. Rebuild y push de NCT + Pool + Worker-GPU
2. Purge de colas viejas en RabbitMQ
3. `pilar3/README.md`
4. Video de demostración
5. CI/CD (`.github/workflows/`)

---

## Riesgos

| Riesgo | Estado |
|---|---|
| Workers no validan TLS | ✅ Resuelto — certbot wildcard en trust store Ubuntu |
| Rate limits GCP Free Trial | ⚠️ Monitorear (4 vCPUs, PD standard) |
| Tiempo | ⚠️ 2 días restantes |
| Conexión AMQP idle durante minado | ✅ Fix implementado (try/except + reconexión en NCT block_loop y result_loop) |
| Pool loop infinito en reconexión | ✅ Fix implementado (`auto_ack=False` en results consumer) |
| Worker requeue infinito en MinerError | ✅ Fix implementado (dead-letter en vez de requeue) |
| Binary CUDA falla silenciosamente | ✅ Fix implementado (stderr a ERROR + incluido en MinerError) |

---

## Decisiones de diseño

| # | Decisión | Fundamento |
|---|---|---|
| D1 | Mismo repositorio para infra y código | Consigna |
| D2 | OpenTofu solo GCP, kubectl para K8s | Sin chicken-and-egg |
| D3 | AMQPS con certbot wildcard (Let's Encrypt) | Sin distribución de CA. Workers validan contra trust store del sistema. |
| D4 | LoadBalancer solo en RabbitMQ | Único servicio externo |
| D5 | NCT singleton | Coordinador único |
| D6 | Workload Identity | Zero static keys |
| D7 | Redis StatefulSet con PVC | Persistencia AOF |
| D8 | Sin NetworkPolicy | Docker no las necesita |
| D9 | Secretos `.example` + gitignore | Templates versionados |
| D10 | cert-manager + nginx-ingress vía kubectl | Sin Helm ni Terraform |
| D11 | Let's Encrypt production para Ingress | Un solo certificado |
| D12 | RabbitMQ TLS verify_peer con cacertfile Alpine | Let's Encrypt en trust store |
| D13 | initContainer fix-cookie en RabbitMQ | `fsGroup` rompe permisos del cookie |
| D14 | Ingress en `apps`, servicios internos vía port-forward | Superficie HTTPS mínima |
| D15 | Dominios separados: `edutokens.xyz` (HTTPS), `rabbitmq.edutokens.xyz` (AMQPS) | Separación de responsabilidades |
