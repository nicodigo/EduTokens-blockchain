# Plan de Migración: Docker Compose → GKE

> **Proyecto:** EduTokens — Blockchain Distribuida y CUDA  
> **Curso:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
> **Última actualización:** 2026-06-19  
> **Deadline entrega:** 2026-06-23

---

## Estado general

| Fase | Estado |
|---|---|
| Fase 1 — Build & Push de imágenes | ⏳ pendiente |
| Fase 2 — Infraestructura (OpenTofu) | ✅ `tofu apply` ejecutado, esperando completar |
| Fase 3 — Manifiestos Kubernetes | ✅ 19 archivos YAML creados en `pilar3/k8s/` |
| Fase 4 — Workers GPU | 🔒 bloqueado (falta info del profesor) |
| Fase 5 — CI/CD | ⏳ pendiente |
| Fase 6 — Observabilidad | 📎 diferida |
| Fase 7 — Verificación | ⏳ pendiente |

---

## Arquitectura

```
┌──────────────────────────────────────────────────────────────────────┐
│  GKE Cluster — us-central1-a (zonal, free tier)                      │
│                                                                       │
│  ┌── namespace: infra ───────────────────────────────────────────┐   │
│  │  Redis (StatefulSet ×1)       RabbitMQ (StatefulSet ×1)        │   │
│  │  PVC 10Gi, AOF               PVC 10Gi                          │   │
│  │  ClusterIP :6379             TLS: cert-manager (Let's Encrypt) │   │
│  │                              LoadBalancer + IP estática        │   │
│  │                              :5672 AMQP (interno)              │   │
│  │                              :5671 AMQPS (workers externos)    │   │
│  │                              :15672 management (vía Ingress)   │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── namespace: blockchain ───────────────────────────────────────┐   │
│  │  NCT (Deployment ×1)          Pool-A (Deployment ×1)            │   │
│  │  ClusterIP :8080             ClusterIP :8090                   │   │
│  │  → amqp://rabbitmq.infra     → amqp://rabbitmq.infra            │   │
│  │  → redis://redis.infra       Sin IP pública                    │   │
│  │  Sin IP pública              KSA: blockchain (Workload ID)     │   │
│  │  KSA: blockchain                                                │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  Ingress (nginx) + cert-manager                                       │
│  ─ rabbitmq.edutokens.duckdns.org → infra/rabbitmq:15672             │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ AMQPS :5671 (TLS)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Cluster GPU (profesor) — pendiente de info                          │
│  Workers con nvidia.com/gpu: 1 → rabbitmq.edutokens.duckdns.org:5671│
└──────────────────────────────────────────────────────────────────────┘
```

---

## Flujo de despliegue (dos fases)

OpenTofu maneja **solo infraestructura GCP**. Kubernetes se maneja con **kubectl**. Esta separación evita el problema de chicken-and-egg y hace explícito lo que pasa en cada paso.

```bash
# ── Fase A: Infraestructura GCP (OpenTofu) ──
cd pilar3/tofu
tofu init
tofu plan
tofu apply          # ← ya ejecutado, esperando completar

# ── Fase B: Componentes del cluster (kubectl) ──
gcloud container clusters get-credentials edutokens-cluster \
  --zone us-central1-a --project edutokens-2026

# 1. Instalar controladores
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml

# 2. Esperar a que estén listos
kubectl -n cert-manager rollout status deployment/cert-manager --timeout=120s
kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=120s

# 3. Crear secretos (copiar desde .example y completar)
cp pilar3/k8s/blockchain/secret.yaml.example     pilar3/k8s/blockchain/secret.yaml
cp pilar3/k8s/infra/rabbitmq-secret.yaml.example  pilar3/k8s/infra/rabbitmq-secret.yaml
# Editar ambos con valores reales
# Completar <GSA_EMAIL> en service-account.yaml con: tofu output gke_pull_service_account
# Completar <LETSENCRYPT_EMAIL> en cluster-issuer.yaml
# Completar loadBalancerIP en rabbitmq-service.yaml con: tofu output rabbitmq_static_ip

# 4. Aplicar todo
kubectl apply -f pilar3/k8s/
```

---

## Fase 1 — Build y Push de imágenes Docker

**Objetivo:** Subir nct, pool, y worker a Artifact Registry.

Artifact Registry ya fue creado por OpenTofu. Solo falta buildear y pushear.

### Imágenes necesarias

| Imagen | Dockerfile | Comando |
|---|---|---|
| `nct` | `pilar2/nct/Dockerfile` | `docker build -f pilar2/nct/Dockerfile -t <REGISTRY>/nct:latest pilar2/` |
| `pool` | `pilar2/pool/Dockerfile` | `docker build -f pilar2/pool/Dockerfile -t <REGISTRY>/pool:latest pilar2/` |
| `worker-cpu` | `pilar2/worker/Dockerfile` | `docker build -f pilar2/worker/Dockerfile -t <REGISTRY>/worker-cpu:latest pilar2/` |

La imagen `worker-gpu` (Fase 4) requiere el binario CUDA y está bloqueada hasta tener info del profesor.

### Paso a paso

```bash
# 1. Autenticarse contra Artifact Registry
gcloud auth configure-docker us-central1-docker.pkg.dev

# 2. Build (ajustar REGISTRY con el output de tofu)
REGISTRY=$(tofu output -raw artifact_registry_url)
cd pilar2
docker build -f nct/Dockerfile    -t $REGISTRY/nct:latest .
docker build -f pool/Dockerfile   -t $REGISTRY/pool:latest .
docker build -f worker/Dockerfile -t $REGISTRY/worker-cpu:latest .

# 3. Push
docker push $REGISTRY/nct:latest
docker push $REGISTRY/pool:latest
docker push $REGISTRY/worker-cpu:latest
```

---

## Fase 2 — Infraestructura OpenTofu ✅

**Ya ejecutado.** Archivos en `pilar3/tofu/`:

| Archivo | Contenido |
|---|---|
| `versions.tf` | Provider google ~6.0 |
| `main.tf` | Config del provider (ADC) — sin kubernetes ni helm |
| `variables.tf` | 12 variables con defaults free-tier |
| `outputs.tf` | IPs, URLs, comandos post-tofu |
| `vpc.tf` | APIs, VPC, subred, Cloud NAT, IP estática |
| `artifact-registry.tf` | Repositorio Docker |
| `iam.tf` | Workload Identity + SA para GitHub Actions |
| `gke.tf` | Cluster zonal, node pool e2-standard-2 ×2 |
| `firewall.tf` | AMQPS desde GPU cluster + SSH vía IAP |
| `cert-manager.tf` | Solo documenta pasos post-tofu con kubectl |
| `terraform.tfvars` | Valores concretos (gitingnored) |
| `terraform.tfvars.example` | Template para nuevos deploys |

### Decisiones free-tier

- Cluster **zonal** (sin costo de plano de control)
- `network_tier = STANDARD` en IP estática
- `pd-standard` (HDD) en nodos
- `monitoring_config = SYSTEM_COMPONENTS` (sin métricas de workload)
- `deletion_protection = false`
- 2 nodos `e2-standard-2` = 4 vCPUs (cuota máxima: 8)

---

## Fase 3 — Manifiestos Kubernetes ✅

**Ya creados.** 19 archivos en `pilar3/k8s/`. No duplicamos contenido acá — ver los archivos directamente.

```
pilar3/k8s/
├── ingress.yaml
├── network-policies.yaml
├── cert-manager/
│   └── cluster-issuer.yaml
├── infra/
│   ├── namespace.yaml
│   ├── redis-statefulset.yaml
│   ├── redis-service.yaml
│   ├── rabbitmq-statefulset.yaml
│   ├── rabbitmq-service.yaml
│   ├── rabbitmq-configmap.yaml
│   ├── rabbitmq-certificate.yaml
│   └── rabbitmq-secret.yaml.example
└── blockchain/
    ├── namespace.yaml
    ├── configmap.yaml
    ├── secret.yaml.example
    ├── service-account.yaml
    ├── nct-deployment.yaml
    ├── nct-service.yaml
    ├── pool-deployment.yaml
    └── pool-service.yaml
```

### Decisiones de diseño

- **NCT singleton** (`replicas: 1`, `strategy: Recreate`). Solo un coordinador decide qué transacciones entran en cada bloque.
- **NetworkPolicy zero-trust**: `infra` solo acepta tráfico de `blockchain`. `blockchain` puede salir a `infra` y a internet.
- **Ingress solo en RabbitMQ Management** por ahora. Los dominios `api.` y `app.` están comentados para cuando existan los backends.
- **Secretos como `.example`**: los valores reales se gitignoranean. Al desplegar: copiar a `.yaml` y completar.
- **Workload Identity**: la KSA `blockchain` se anota con `iam.gke.io/gcp-service-account`. Sin static keys.
- **RabbitMQ TLS**: cert-manager genera el certificado, se monta como volumen en `/etc/rabbitmq/ssl/`. El ConfigMap configura AMQPS en `:5671`.

---

## Fase 4 — Workers GPU 🔒

**Bloqueado.** Necesitamos del profesor:

| Dato | Para qué |
|---|---|
| Rango de IPs de salida del cluster GPU | Configurar `loadBalancerSourceRanges` y firewall |
| Modelo de GPU (ej: T4, A100) | Compilar `md5_range` con `nvcc -arch=sm_XX` |
| Versión de CUDA disponible | Elegir imagen base (`nvidia/cuda:XX.X-runtime`) |
| ¿Kubernetes o acceso SSH? | Saber si usamos manifiestos K8s o systemd |

### Entregables (cuando se desbloquee)

| Archivo | Descripción |
|---|---|
| `pilar3/docker/worker-gpu.Dockerfile` | Imagen con CUDA runtime + binario md5_range |
| `pilar3/k8s/workers/worker-deployment.yaml` | Deployment con `nvidia.com/gpu: 1` |

---

## Fase 5 — CI/CD con GitHub Actions

**Pendiente.** La consigna pide CI/CD. Propuesta mínima y funcional:

### 5.1 Build & Push (en push a main)

```yaml
# .github/workflows/ci.yml
on:
  push:
    branches: [main]
    paths: ['pilar2/**']

jobs:
  build-push:
    steps:
      - checkout
      - gcloud auth (OIDC)
      - docker build + push de nct, pool, worker-cpu
```

### 5.2 gitleaks (en todo push y PR)

```yaml
# .github/workflows/gitleaks.yml
on: [push, pull_request]
jobs:
  scan:
    steps:
      - checkout (fetch-depth: 0)
      - gitleaks/gitleaks-action@v2
```

**No incluimos en CI:** deploy automático a GKE (es manual con `kubectl apply`), ni tofu apply (la infra se crea una vez).

### Workload Identity para GitHub Actions

GitHub Actions se autentica contra GCP vía OIDC (sin service account keys). El setup está en `iam.tf` (SA `github-actions` con roles `artifactregistry.writer` y `container.developer`). Falta crear el Workload Identity Pool y Provider en GCP — esto se hace una vez por consola o con gcloud.

---

## Fase 6 — Observabilidad 📎

**Diferida.** Solo activar si Fases 1-5 están completas y sobra tiempo.

Plan mínimo: agregar `prometheus_client` a los servicios Python para exponer `/metrics`, desplegar `kube-prometheus-stack` vía un `kubectl apply`.

---

## Fase 7 — Verificación

**Pendiente.** Validación end-to-end antes de la entrega.

### 7.1 Health checks

```bash
kubectl -n infra       port-forward svc/rabbitmq 15672:15672 &
kubectl -n blockchain  port-forward svc/nct      8080:8080 &
curl -f localhost:8080/health
curl -f localhost:8080/status
```

### 7.2 Transacción de prueba

```bash
# 1. Generar keypair Ed25519
python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
sk = Ed25519PrivateKey.generate()
print('PUBLIC:', sk.public_key().public_bytes_raw().hex())
"

# 2. Firmar y enviar transacción (usar AUTHORITY_PUBKEY para EARN)
curl -X POST localhost:8080/transaction -H 'Content-Type: application/json' -d '{
  "sender_pubkey": "<AUTHORITY_PUBKEY>",
  "receiver_pubkey": "<pubkey_generada>",
  "amount": 100,
  "tx_type": "EARN",
  ...
}'

# 3. Esperar a que se mine un bloque y verificar
curl localhost:8080/chain | python3 -m json.tool | head -50
```

### 7.3 Documentación

- `pilar3/README.md`: instrucciones de despliegue paso a paso
- `project_overview.md`: agregar arquitectura cloud (el diagrama de este documento)
- Video de demostración: `tofu apply`, `kubectl get pods`, `curl /health`, envío de tx, consulta de cadena

---

## Riesgos

| Riesgo | Estado | Mitigación |
|---|---|---|
| Workers no validan TLS de RabbitMQ | 🔒 Bloqueado | Let's Encrypt está en el trust store de Ubuntu. Si la imagen GPU no lo tiene, montar el CA cert manualmente. |
| `md5_range` compilado para sm_75 no corre en la GPU del profesor | 🔒 Bloqueado | Recompilar para la arquitectura correcta. CPU fallback como plan B. |
| Rate limits de GCP Free Trial ($300 crédito) | ⚠️ Monitorear | Cluster chico (4 vCPUs), PD standard, NAT en vez de IPs por nodo. |
| Tiempo | ⚠️ 4 días | Priorizar Fase 1 → Fase 5 → Fase 7. Diferir Fase 4 y 6. |

---

## Decisiones de diseño

| # | Decisión | Fundamento |
|---|---|---|
| D1 | **Mismo repositorio** para infra y código | Consigna: repositorio único público. Imágenes y K8s acoplados al código. |
| D2 | **OpenTofu solo para GCP**, kubectl para K8s | Evita el chicken-and-egg del provider de Kubernetes. Hace explícita cada fase. |
| D3 | **AMQPS con Let's Encrypt**, sin VPN | Workers en otro cluster administrativo. TLS sobre internet es más simple que VPN site-to-site. |
| D4 | **LoadBalancer solo en RabbitMQ** | RabbitMQ necesita ser alcanzable desde fuera. NCT se accede vía Ingress desde el frontend. |
| D5 | **NCT singleton** (`replicas: 1`) | Coordinador único de la blockchain. Leader election sería overkill para el TP. |
| D6 | **Workload Identity** (zero static keys) | Cumple la consigna. Los pods se autentican automáticamente contra GCP. |
| D7 | **Redis StatefulSet con PVC** | AOF persistence requiere volumen persistente con identidad estable. |
| D8 | **NetworkPolicy zero-trust** | Minimiza blast radius si un namespace es comprometido. |
| D9 | **Secretos como `.example` + gitignore** | Templates versionados, valores reales nunca commiteados. Profesional y seguro. |
| D10 | **cert-manager + nginx-ingress vía kubectl** | Instalación estándar con manifiestos públicos. Sin dependencia de Helm o Terraform. |
