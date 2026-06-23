# Pilar 3 — Cloud Deployment (GKE)

> **Proyecto:** EduTokens Blockchain — SDyPP, UNLu  
> **Deadline:** 2026-06-23  

Guía paso a paso para desplegar la blockchain EduTokens en Google Kubernetes Engine (GKE) usando OpenTofu como Infrastructure as Code.

---

## Arquitectura

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
│  │  nginx-ingress + certbot wildcard TLS (*.edutokens.xyz)          │   │
│  │  Dominio: edutokens.xyz                                         │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌── cluster externo (profesor): namespace g-compumundo ─────────┐   │
│  │  Worker GPU (RTX 4060, sm_89, CUDA 12.2)                       │   │
│  │  → AMQPS a rabbitmq.edutokens.xyz:5671                         │   │
│  └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Prerrequisitos

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud` autenticado)
- [OpenTofu](https://opentofu.org/docs/intro/install/) ≥ 1.6.0
- [kubectl](https://kubernetes.io/docs/tasks/tools/) ≥ 1.27
- [Docker](https://docs.docker.com/engine/install/) (para build de imágenes)
- Proyecto GCP con billing activado (`edutokens-2026`)
- Dominio `edutokens.xyz` (o similar) con acceso a DNS

---

## 1. Infraestructura (OpenTofu)

### 1.1 Configurar variables

```bash
cd pilar3/tofu
cp terraform.tfvars.example terraform.tfvars
# Editar terraform.tfvars con tus valores:
#   domain_name        = "edutokens.xyz"
#   letsencrypt_email  = "tu-email@ejemplo.com"
```

### 1.2 Aplicar

```bash
tofu init
tofu plan
tofu apply
```

**Recursos creados:**
- GKE cluster (`edutokens-cluster`, us-central1-a)
- VPC + subred + Cloud NAT
- Artifact Registry (`edutokens-repo`)
- 2 IPs estáticas PREMIUM (RabbitMQ, nginx-ingress)
- Workload Identity (SA `gke-pull-images` para pods GKE)
- Workload Identity Federation (SA `github-actions` para CI/CD vía OIDC)
- IAM bindings para ambos service accounts

**Salidas útiles después de apply:**
```bash
tofu output artifact_registry_url    # us-central1-docker.pkg.dev/edutokens-2026/edutokens-repo
tofu output get_credentials_command  # gcloud container clusters get-credentials ...
tofu output rabbitmq_static_ip       # IP para registro DNS de rabbitmq.edutokens.xyz
```

---

## 2. Conectarse al cluster

```bash
gcloud container clusters get-credentials edutokens-cluster \
  --zone us-central1-a --project edutokens-2026
```

---

## 3. Instalar ingress-nginx + Configurar TLS

```bash
# nginx-ingress como controlador de entrada
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml
```

### 3.1 Certificado TLS (certbot wildcard DNS-01)

El certificado wildcard `*.edutokens.xyz` se genera con certbot usando DNS-01
challenge vía Namecheap. Cubre `edutokens.xyz`, `nct.edutokens.xyz` y cualquier
subdominio futuro **sin necesidad de cert-manager**.

Crear el Secret TLS desde los archivos de certbot:

```bash
sudo kubectl create secret tls edutokens-tls \
    --cert=/etc/letsencrypt/live/edutokens.xyz/fullchain.pem \
    --key=/etc/letsencrypt/live/edutokens.xyz/privkey.pem \
    -n apps --dry-run=client -o yaml | kubectl apply -f -
```

⚠️ El certificado expira cada 90 días. Renovar con:
```bash
sudo certbot renew
# Luego repetir el kubectl create secret tls de arriba
```

---

## 4. Configurar secretos

### 4.1 Certificado TLS para RabbitMQ (AMQPS)

```bash
kubectl create namespace infra --dry-run=client -o yaml | kubectl apply -f -

kubectl -n infra create secret generic rabbitmq-amqps-tls \
  --from-file=tls.crt=/ruta/a/fullchain.pem \
  --from-file=tls.key=/ruta/a/privkey.pem
```

### 4.2 Secretos de aplicación

```bash
cp pilar3/k8s/blockchain/secret.yaml.example    pilar3/k8s/blockchain/secret.yaml
cp pilar3/k8s/infra/rabbitmq-secret.yaml.example pilar3/k8s/infra/rabbitmq-secret.yaml
cp pilar3/k8s/workers/worker-gpu-secret.yaml.example pilar3/k8s/workers/worker-gpu-secret.yaml
# Completar los valores en cada secret.yaml
```

**Nota:** Los archivos `secret.yaml` están en `.gitignore`. Nunca se commitean.

---

## 5. Build y push de imágenes Docker

```bash
# Autenticarse contra Artifact Registry
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

# Build + push de las 4 imágenes
./pilar3/scripts/build-push.sh
```

**O automáticamente:** el workflow `ci.yml` de GitHub Actions hace build + push en cada push a `main`.

| Imagen | Dockerfile | Build context |
|---|---|---|
| `nct:latest` | `pilar2/nct/Dockerfile` | `pilar2/` |
| `pool:latest` | `pilar2/pool/Dockerfile` | `pilar2/` |
| `worker-cpu:latest` | `pilar2/worker/Dockerfile` | `pilar2/` |
| `worker-gpu:latest` | `pilar3/docker/worker-gpu.Dockerfile` | repo root |

---

## 6. Desplegar en Kubernetes

```bash
./pilar3/scripts/deploy-k8s.sh
```

**Qué aplica (en orden):**
1. Namespaces: `infra`, `blockchain`, `apps`
2. ConfigMaps + Secrets + ServiceAccount + ClusterIssuer
3. Services (Redis, RabbitMQ, NCT, Pool)
4. StatefulSets (Redis, RabbitMQ) + Deployments (NCT, Pool)
5. Ingress (HTTPS con Let's Encrypt production)

**Verificar que todo esté listo:**
```bash
kubectl get pods -n infra
kubectl get pods -n blockchain
```

---

## 7. Workers GPU (cluster del profesor)

Los workers corren en el cluster GPU del profesor (namespace `g-compumundo`), conectándose vía AMQPS a `rabbitmq.edutokens.xyz:5671`.

### 7.1 Desplegar

```bash
cp pilar3/k8s/workers/worker-gpu-secret.yaml.example pilar3/k8s/workers/worker-gpu-secret.yaml
# Completar AMQP_URL con la URL de conexión AMQPS

kubectl apply -f pilar3/k8s/workers/
```

### 7.2 Rebuild después de cambios de código

```bash
docker build -f pilar3/docker/worker-gpu.Dockerfile \
  -t us-central1-docker.pkg.dev/edutokens-2026/edutokens-repo/worker-gpu:latest . \
  && docker push $_

kubectl -n g-compumundo rollout restart deployment worker-gpu
```

---

## 8. Verificación

```bash
# Port-forward al NCT
kubectl -n blockchain port-forward svc/nct 8080:8080 &

# Health check
curl localhost:8080/health
# → {"status":"ok"}

# Estado de la cadena
curl localhost:8080/status
# → {"chain_height":2,"pending_transactions":0,"current_block":null,"active_pools":1}

# Enviar transacción de prueba
python3 pilar2/tools/send_test_tx.py gen          # genera keypair
python3 pilar2/tools/send_test_tx.py earn \
  <authority_privkey> <student_pubkey> 1000 "test"
```

---

## 9. CI/CD (GitHub Actions)

Dos workflows automáticos en `.github/workflows/`:

| Workflow | Trigger | Qué hace |
|---|---|---|
| `gitleaks.yml` | push + PR a `main` | Escanea todo el historial en busca de secretos |
| `ci.yml` | push a `main` | Build + push de las 4 imágenes a Artifact Registry |
| `ci.yml` | PR a `main` | Solo build (verifica que compilan, sin pushear) |

La autenticación a GCP usa Workload Identity Federation (OIDC) — cero service account keys.

---

## 10. Comandos útiles

```bash
# Logs
kubectl -n blockchain logs deploy/nct --tail=50
kubectl -n blockchain logs deploy/pool-a --tail=50

# Reiniciar un deployment (después de pushear nueva imagen)
kubectl -n blockchain rollout restart deployment nct
kubectl -n blockchain rollout restart deployment pool-a

# RabbitMQ Management UI
kubectl -n infra port-forward svc/rabbitmq 15672:15672 &
# Abrir http://localhost:15672 (guest/guest)

# Limpiar colas RabbitMQ
kubectl -n infra exec rabbitmq-0 -- rabbitmqctl purge_queue pool.pool-a.inbox
kubectl -n infra exec rabbitmq-0 -- rabbitmqctl purge_queue pool.pool-a.results
kubectl -n infra exec rabbitmq-0 -- rabbitmqctl purge_queue pool.pool-a.tasks

# OpenTofu (ver cambios pendientes en infra)
cd pilar3/tofu && tofu plan
```

---

## Estructura de directorios

```
pilar3/
├── README.md                    ← este archivo
├── tofu/                        # OpenTofu (IaC)
│   ├── main.tf                  # Provider Google
│   ├── variables.tf             # Variables de entrada
│   ├── terraform.tfvars.example # Template de valores
│   ├── vpc.tf                   # VPC + subred + Cloud NAT
│   ├── gke.tf                   # GKE cluster + node pool
│   ├── artifact-registry.tf     # Artifact Registry Docker
│   ├── iam.tf                   # Service accounts + IAM + Workload Identity Federation
│   ├── versions.tf              # Provider versions
│   └── outputs.tf               # Outputs (IPs, URLs, comandos)
├── k8s/                         # Manifiestos Kubernetes (16 YAMLs)
│   ├── ingress.yaml             # Ingress HTTPS (apps namespace, certbot TLS)
│   ├── network-policies.yaml    # NetworkPolicy (deshabilitado en prod)
│   ├── nct-external-service.yaml  # ExternalName: apps → blockchain
│   ├── apps/
│   │   └── namespace.yaml
│   ├── infra/
│   │   ├── namespace.yaml
│   │   ├── redis-*.yaml         # StatefulSet + Service (AOF, PVC 10Gi)
│   │   ├── rabbitmq-*.yaml      # StatefulSet (TLS AMQPS) + Service (LoadBalancer)
│   │   └── rabbitmq-secret.yaml.example
│   ├── blockchain/
│   │   ├── namespace.yaml
│   │   ├── configmap.yaml       # BLOCK_SIZE, DIFFICULTY, etc.
│   │   ├── secret.yaml.example
│   │   ├── service-account.yaml # KSA para Workload Identity
│   │   ├── nct-*.yaml           # Deployment singleton + ClusterIP
│   │   └── pool-*.yaml          # Deployment + ClusterIP
│   └── workers/
│       ├── worker-gpu-deployment.yaml # GPU worker (nvidia.com/gpu: 1)
│       └── worker-gpu-secret.yaml.example
├── docker/
│   └── worker-gpu.Dockerfile    # Worker GPU (nvidia/cuda:12.2.2-runtime-ubuntu22.04)
├── scripts/
│   ├── build-push.sh            # Build + push de 4 imágenes a Artifact Registry
│   └── deploy-k8s.sh            # Apply ordenado de todos los manifiestos
└── .artifacts/                  # Documentación interna de sesiones anteriores
    ├── handoff-2026-06-20.md
    ├── handoff-2026-06-20-v2.md
    └── cloud-migration-plan.md
```

---

## Decisiones de diseño cloud

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
| D10 | certbot wildcard DNS-01 (Namecheap) + nginx-ingress | Sin cert-manager, renovación manual cada 90 días |
| D11 | Let's Encrypt production para Ingress | Un solo certificado wildcard |
| D12 | Dominios separados: `edutokens.xyz` (HTTPS), `rabbitmq.edutokens.xyz` (AMQPS) | Separación de responsabilidades |
| D13 | CI/CD con Workload Identity Federation | GitHub Actions → GCP sin credenciales estáticas |
| D14 | GitHub Actions cache para Docker builds | Rebuilds incrementales, solo cambia lo modificado |
