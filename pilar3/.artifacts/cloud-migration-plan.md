# Plan de Migración: Docker Compose → GKE

> **Proyecto:** EduTokens — Blockchain Distribuida y CUDA  
> **Curso:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
> **Última actualización:** 2026-06-20 (sesión 3: CI/CD completado, fixes deployados y verificados, documentación actualizada)  
> **Deadline entrega:** 2026-06-23

---

## Estado general

| Fase | Estado |
|---|---|
| Fase 1 — Build & Push de imágenes | ✅ completado |
| Fase 2 — Infraestructura (OpenTofu) | ✅ aplicado |
| Fase 3 — Manifiestos Kubernetes | ✅ funcionando |
| Fase 4 — Workers GPU | ✅ desplegado, conectado, minado validado |
| Fase 5 — CI/CD | ✅ completado (Workload Identity Federation + gitleaks.yml + ci.yml) |
| Fase 6 — Observabilidad | 📎 diferida |
| Fase 7 — Verificación | ✅ completada (NCT + Pool redeployados con fixes, EARN tx aceptada, 0 errores en logs). Ver `pilar3/.artifacts/handoff-2026-06-20-v3.md` |

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

## Fase 5 — CI/CD ✅

**Completado en sesión 3.** Workflows creados y funcionando en GitHub Actions:

| Workflow | Archivo | Trigger | Acción |
|---|---|---|---|
| Gitleaks | `.github/workflows/gitleaks.yml` | push + PR a `main` | Escanea historial completo con gitleaks v8.27.0 |
| CI — Build & Push | `.github/workflows/ci.yml` | push a `main` | Build + push de 4 imágenes a Artifact Registry |
| CI — Build & Push | `.github/workflows/ci.yml` | PR a `main` | Solo build (verifica compilación) |

**Autenticación:** Workload Identity Federation vía OIDC. Pool `github-actions-oidc` + provider `github-actions-provider` creados en `pilar3/tofu/iam.tf`. El SA `github-actions` tiene `artifactregistry.writer` y `container.developer`. Docker builds usan GitHub Actions cache (`type=gha`) con driver Buildx container.

**Nota:** El `attribute_condition` en el provider OIDC es **obligatorio** (la API de GCP lo exige aunque la doc diga que es opcional). Debe usar claims raw del token (`assertion.repository`), no atributos mapeados.

---

## Fase 6 — Observabilidad 📎

**Diferida.** Solo si sobra tiempo.

---

## Fase 7 — Verificación ✅

**Completada en sesión 3.** Los 5 fixes fueron deployados y verificados:

| Fix | Archivos | Estado |
|---|---|---|
| NCT `StreamLostError` crash al publicar bloque | `nct/nct.py` — try/except con reconexión | ✅ Deployado y verificado |
| Worker `MinerError: empty stdout` — requeue infinito | `miner/miner.py` + `worker/worker.py` | ✅ Deployado y verificado |
| Pool `PRECONDITION_FAILED` loop infinito | `pool/pool.py` — `auto_ack=False` | ✅ Deployado y verificado |
| `send_test_tx.py earn` nonce=0 siempre | `tools/send_test_tx.py` | ✅ Deployado y verificado |
| `get_discarded_txns` crashea con `.decode()` | `storage/chain_store.py` | ✅ Deployado y verificado |

**Verificación realizada:**
1. Rebuild + push de NCT y Pool (`docker build` + `docker push`)
2. Rollout restart en namespace `blockchain` (NCT + Pool-A)
3. Purge de colas RabbitMQ (`pool.pool-a.inbox`, `.results`, `.tasks`)
4. Port-forward NCT → `curl /health` → `{"status":"ok"}`
5. `curl /status` → `{"chain_height":1,"pending_transactions":0,"active_pools":1}`
6. `send_test_tx.py earn` → tx aceptada (201 Created), tx_id en mempool
7. Logs de NCT y Pool: **cero errores**

**Pendiente:**
1. Video de demostración
2. Worker GPU deploy (realizado por el usuario en su cluster)

---

## Riesgos

| Riesgo | Estado |
|---|---|
| Workers no validan TLS | ✅ Resuelto — certbot wildcard en trust store Ubuntu |
| Rate limits GCP Free Trial | ⚠️ Monitorear (4 vCPUs, PD standard) |
| Tiempo | ⚠️ 2 días restantes |
| Conexión AMQP idle durante minado | ✅ Deployado y verificado (try/except + reconexión en NCT) |
| Pool loop infinito en reconexión | ✅ Deployado y verificado (`auto_ack=False`) |
| Worker requeue infinito en MinerError | ✅ Deployado y verificado (dead-letter) |
| Binary CUDA falla silenciosamente | ✅ Deployado y verificado (stderr a ERROR) |
| CI/CD sin autenticación a GCP | ✅ Resuelto — Workload Identity Federation OIDC |

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
| D16 | CI/CD con Workload Identity Federation OIDC | GitHub Actions → GCP sin credenciales estáticas |
| D17 | GitHub Actions cache (`type=gha`) + Buildx container driver | Builds incrementales, solo cambia lo modificado |
| D18 | gitleaks standalone binary (no action externa) | Sin dependencia de licencias, funciona en repos personales |
