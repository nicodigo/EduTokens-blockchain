# Plan de Migración: Docker Compose → GKE

> **Proyecto:** EduTokens — Blockchain Distribuida y CUDA  
> **Curso:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
> **Fecha del plan:** 2026-06-19  
> **Deadline entrega:** 2026-06-23  
> **Estado:** ⏳ Plan confirmado, implementación pendiente

---

## Arquitectura objetivo

```
┌─────────────────────────────────────────────────────────────────────────┐
│  GKE Cluster (nuestro) — región us-central1                             │
│                                                                          │
│  ┌── namespace: infra ──────────────────────────────────────────────┐   │
│  │                                                                    │   │
│  │  Redis (StatefulSet)             RabbitMQ (StatefulSet)           │   │
│  │  PVC 10Gi, AOF                  PVC 10Gi                          │   │
│  │  Service: ClusterIP             TLS: cert-manager (Let's Encrypt) │   │
│  │  Port: 6379                     Service: LoadBalancer (IP fija)   │   │
│  │                                 Ports:                            │   │
│  │                                 ─ 5672  AMQP   (interno NCT/Pool) │   │
│  │                                 ─ 5671  AMQPS  (externo Workers)  │   │
│  │                                 Ingress: :443 → Management :15672 │   │
│  └──────────────┬──────────────────────────────────────────────────┘   │
│                 │                                                       │
│  ┌── namespace: blockchain ─────────────────────────────────────────┐   │
│  │                                                                    │   │
│  │  NCT (Deployment ×1, singleton)     Pool-A (Deployment ×1)        │   │
│  │  ─────────────────────────────      ──────────────────────        │   │
│  │  → amqp://rabbitmq.infra:5672       → amqp://rabbitmq.infra:5672  │   │
│  │  → redis://redis.infra:6379         Health :8090                  │   │
│  │  Health :8080                                                     │   │
│  │  Service: ClusterIP (sin IP pública)                              │   │
│  │  Accedido vía Ingress desde apps                                  │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  Ingress (nginx o GCE)                                                   │
│  ─ certificado Let's Encrypt vía cert-manager                            │
│  ─ rabbitmq.edutokens.duckdns.org  → infra/rabbitmq:15672               │
│  ─ api.edutokens.duckdns.org       → blockchain/nct:8080 (futuro)       │
│  ─ app.edutokens.duckdns.org       → apps/frontend:80  (futuro)         │
│                                                                          │
│  ┌── namespace: observability (fase diferida) ───────────────────────┐   │
│  │  Prometheus + Grafana (ServiceMonitor scraping /metrics endpoints) │   │
│  └───────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ AMQPS (TLS, :5671)
                               │ sobre internet
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Cluster GPU (profesor) — nodegroup con GPUs NVIDIA                      │
│                                                                          │
│  ┌── namespace: workers ─────────────────────────────────────────────┐   │
│  │  Worker GPU (Deployment)                                           │   │
│  │  ────────────────────────                                          │   │
│  │  imagen: worker-gpu (nvidia/cuda:12.8-runtime)                     │   │
│  │  MINER_BINARY=./md5_range  (binario CUDA compilado)                │   │
│  │  RABBITMQ_URL=amqps://rabbitmq.edutokens.duckdns.org:5671/        │   │
│  │  Health :8081                                                      │   │
│  │  Resource: nvidia.com/gpu: 1                                       │   │
│  └────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Diferencias clave con docker-compose local

| Aspecto | Local (docker-compose) | Cloud (GKE) |
|---|---|---|
| **NCT → RabbitMQ** | `amqp://rabbitmq:5672` | `amqp://rabbitmq.infra.svc.cluster.local:5672` |
| **Pool → RabbitMQ** | `amqp://rabbitmq:5672` | `amqp://rabbitmq.infra.svc.cluster.local:5672` |
| **Worker → RabbitMQ** | `amqp://rabbitmq:5672` (misma red) | `amqps://rabbitmq.edutokens.duckdns.org:5671` (TLS sobre internet) |
| **Redis** | Volumen Docker local | PersistentVolumeClaim (GCE Persistent Disk, 10Gi) |
| **Secretos** | `.env` montado como volumen | Kubernetes Secrets + referencias a env vars |
| **Workers** | Contenedores en misma red | Cluster remoto, conectividad por internet con TLS |
| **Imágenes** | Build local (`docker compose build`) | Artifact Registry, pull via Workload Identity |
| **Escalado** | `deploy.replicas: 2` estático | Deployments con `replicas`, preparado para HPA futuro |
| **NCT IP pública** | No (localhost) | No (ClusterIP, accedido vía Ingress desde el frontend) |
| **RabbitMQ Management** | `localhost:15672` | `rabbitmq.edutokens.duckdns.org` vía Ingress + TLS |
| **Health endpoints** | `:8080/health`, `:8090/health`, `:8081/health` | Ídem, accedidos internamente o vía Ingress |

---

## Fase 1 — Preparación de imágenes y Artifact Registry

**Objetivo:** Subir las imágenes Docker a GCP para que GKE pueda pullearlas sin imagePullSecrets.

### 1.1 Crear proyecto GCP y Artifact Registry

- Crear proyecto `edutokens` en GCP (o el nombre que elijas)
- Habilitar APIs: `artifactregistry.googleapis.com`, `container.googleapis.com`, `compute.googleapis.com`, `certificatemanager.googleapis.com`
- Crear repositorio Docker en Artifact Registry: `us-central1-docker.pkg.dev/edutokens/edutokens-repo`
- Configurar `gcloud auth configure-docker us-central1-docker.pkg.dev`

### 1.2 Verificar Dockerfiles existentes

Los Dockerfiles en `pilar2/nct/Dockerfile`, `pilar2/pool/Dockerfile`, y `pilar2/worker/Dockerfile` ya son aptos para K8s:
- Usan `python:3.12-alpine` (imagen ligera, ~50MB comprimida)
- Tienen `curl` para healthchecks
- Ejecutan el servicio como `CMD ["python", "-m", "nct.nct"]` (proceso en foreground)
- No requieren cambios para el despliegue cloud

⚠️ **Excepción:** El worker para GPU necesita un Dockerfile nuevo (`pilar3/docker/worker-gpu.Dockerfile`) basado en `nvidia/cuda:12.8-runtime-ubuntu22.04` para tener acceso al runtime CUDA y al binario `md5_range`.

### 1.3 Workload Identity para GKE → Artifact Registry

- Crear service account de GCP: `gke-pull-images@edutokens.iam.gserviceaccount.com`
- Asignar rol: `roles/artifactregistry.reader`
- Configurar Workload Identity en GKE para mapear la KSA `blockchain` a la GSA `gke-pull-images`
- **Zero static keys**: los pods se autentican automáticamente contra GCP sin credenciales hardcodeadas

### Entregables Fase 1

| Archivo | Descripción |
|---|---|
| `pilar3/tofu/artifact-registry.tf` | Creación del repositorio Docker en Artifact Registry |
| `pilar3/tofu/iam.tf` | Service accounts y bindings de Workload Identity |
| `pilar3/docker/worker-gpu.Dockerfile` | Dockerfile para worker con soporte CUDA |
| `.github/workflows/build-push.yml` | Pipeline para build y push de las 4 imágenes |

---

## Fase 2 — Infraestructura como Código (OpenTofu)

**Objetivo:** Provisionar el cluster GKE, VPC, firewall, IP estática, y cert-manager con OpenTofu. Zero clicks en consola.

### 2.1 VPC y subred

- VPC: `edutokens-vpc`
- Subred: `us-central1` con rango `10.0.0.0/20`
- Cloud NAT para tráfico de salida de los pods (necesario para alcanzar Let's Encrypt, Artifact Registry, etc.)
- Cloud Router asociado al NAT

### 2.2 GKE Cluster

- Cluster regional (`us-central1`), canal de release `REGULAR`
- Workload Identity habilitado
- Network policy habilitado (Calico o Cilium, necesario para Fase 3.3)
- Modo: `VPC-native` (alias IP)
- Rango de pods: `10.1.0.0/16`, rango de servicios: `10.2.0.0/20`

### 2.3 Node pools

| Pool | Tipo de máquina | Nodos | Propósito |
|---|---|---|---|
| `infra-apps` | `e2-standard-2` (2vCPU, 8GB) | 2 | Redis, RabbitMQ, NCT, Pool |
| `observability` | `e2-small` (1vCPU, 2GB) | 1 | Prometheus, Grafana (Fase 6) |

Nota: No necesitamos node pool GPU en nuestro cluster. Los workers GPU corren en el cluster del profesor.

### 2.4 Firewall rules

| Regla | Origen | Puerto | Destino | Propósito |
|---|---|---|---|---|
| `allow-amqps-from-gpu` | IPs del cluster GPU del profesor | `5671/tcp` | RabbitMQ LoadBalancer | Workers → RabbitMQ |
| `allow-rmq-mgmt` | IPs del equipo de desarrollo | `15672/tcp` | RabbitMQ (vía Ingress) | Management UI |
| `allow-http-ingress` | `0.0.0.0/0` | `443/tcp` | Ingress Controller | Acceso público a frontend/NCT |
| `deny-external-redis` | — | `6379/tcp` | — | Bloquear acceso externo a Redis |
| `deny-external-nct` | — | `8080/tcp` | — | NCT solo accesible vía Ingress |

### 2.5 IP estática para RabbitMQ

- `google_compute_global_address` o `google_compute_address` regional
- Nombre: `rabbitmq-static-ip`
- Esta IP se asocia al Service `LoadBalancer` de RabbitMQ (Fase 3.1b)
- Garantiza que los workers siempre apunten a la misma IP aunque se recre el Service

### 2.6 cert-manager

- Instalar vía Helm provider de Terraform (`helm_release`)
- Crear `ClusterIssuer` con Let's Encrypt (staging primero, production después)
- Método de validación: `http01` (requiere que el Ingress esté operativo)

### Entregables Fase 2

| Archivo | Descripción |
|---|---|
| `pilar3/tofu/versions.tf` | Providers requeridos (google, kubernetes, helm, random) |
| `pilar3/tofu/main.tf` | Configuración del provider de Google |
| `pilar3/tofu/variables.tf` | Variables: `project_id`, `region`, `cluster_name`, `domain_name`, `gpu_source_ranges` |
| `pilar3/tofu/outputs.tf` | Outputs: `cluster_endpoint`, `rabbitmq_static_ip`, `artifact_registry_url` |
| `pilar3/tofu/vpc.tf` | VPC, subred, Cloud NAT, Cloud Router, firewall rules |
| `pilar3/tofu/gke.tf` | Cluster GKE, node pools, Workload Identity |
| `pilar3/tofu/cert-manager.tf` | Instalación de cert-manager, ClusterIssuer Let's Encrypt |
| `pilar3/tofu/terraform.tfvars.example` | Valores de ejemplo para las variables |

---

## Fase 3 — Manifiestos Kubernetes

**Objetivo:** Traducir `docker-compose.yml` a manifiestos K8s con namespaces, probes, resource limits, network policies, y secretos.

### 3.1 Namespace `infra` — Redis y RabbitMQ

#### 3.1a Redis StatefulSet

```yaml
# pilar3/k8s/infra/redis-statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  namespace: infra
spec:
  serviceName: redis
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        args: ["redis-server", "--appendonly", "yes"]
        ports:
        - containerPort: 6379
        resources:
          requests: { memory: "128Mi", cpu: "250m" }
          limits:   { memory: "256Mi", cpu: "500m" }
        livenessProbe:
          exec:
            command: ["redis-cli", "ping"]
          initialDelaySeconds: 10
          periodSeconds: 10
        readinessProbe:
          exec:
            command: ["redis-cli", "ping"]
          initialDelaySeconds: 5
          periodSeconds: 5
        volumeMounts:
        - name: redis-data
          mountPath: /data
  volumeClaimTemplates:
  - metadata:
      name: redis-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi
```

#### 3.1b Redis Service

```yaml
# pilar3/k8s/infra/redis-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: infra
spec:
  type: ClusterIP
  selector:
    app: redis
  ports:
  - port: 6379
    targetPort: 6379
```

#### 3.1c RabbitMQ StatefulSet (con TLS)

Configuración de RabbitMQ para AMQPS:

```ini
# rabbitmq.conf (montado como ConfigMap)
listeners.tcp.default = 5672
listeners.ssl.default = 5671
ssl_options.cacertfile = /etc/rabbitmq/ssl/ca.crt
ssl_options.certfile   = /etc/rabbitmq/ssl/tls.crt
ssl_options.keyfile    = /etc/rabbitmq/ssl/tls.key
ssl_options.verify     = verify_peer
ssl_options.fail_if_no_peer_cert = false
management.tcp.port    = 15672
```

El secret TLS (`rabbitmq-tls`) generado por cert-manager se monta en `/etc/rabbitmq/ssl/`. El StatefulSet configura:
- `securityContext` para que RabbitMQ pueda leer los archivos de certificado
- PVC de 10Gi para persistencia de colas y mensajes
- Healthcheck: `rabbitmq-diagnostics check_port_connectivity`
- Variables de entorno por defecto: `RABBITMQ_DEFAULT_USER=admin`, contraseña desde Secret

#### 3.1d RabbitMQ Service (LoadBalancer con IP estática)

```yaml
# pilar3/k8s/infra/rabbitmq-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: rabbitmq
  namespace: infra
  annotations:
    # Asocia la IP estática reservada en Fase 2.5
    # (el valor exacto depende del provider de GCP)
spec:
  type: LoadBalancer
  loadBalancerIP: "<IP_RESERVADA>"
  selector:
    app: rabbitmq
  ports:
  - name: amqp
    port: 5672
    targetPort: 5672
  - name: amqps
    port: 5671
    targetPort: 5671
  - name: management
    port: 15672
    targetPort: 15672
```

#### 3.1e RabbitMQ Certificate (cert-manager)

```yaml
# pilar3/k8s/infra/rabbitmq-certificate.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: rabbitmq-tls
  namespace: infra
spec:
  secretName: rabbitmq-tls
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
  - rabbitmq.edutokens.duckdns.org
```

#### 3.1f RabbitMQ ConfigMap

```yaml
# pilar3/k8s/infra/rabbitmq-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: rabbitmq-config
  namespace: infra
data:
  rabbitmq.conf: |
    listeners.tcp.default = 5672
    listeners.ssl.default = 5671
    ssl_options.cacertfile = /etc/rabbitmq/ssl/ca.crt
    ssl_options.certfile   = /etc/rabbitmq/ssl/tls.crt
    ssl_options.keyfile    = /etc/rabbitmq/ssl/tls.key
    ssl_options.verify     = verify_peer
    ssl_options.fail_if_no_peer_cert = false
    management.tcp.port = 15672
```

### 3.2 Namespace `blockchain` — NCT y Pool

#### 3.2a ConfigMap

```yaml
# pilar3/k8s/blockchain/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: blockchain-config
  namespace: blockchain
data:
  BLOCK_SIZE: "5"
  BLOCK_TIMEOUT: "30"
  DIFFICULTY: "4"
  NONCE_SPACE: "1000000000"
  RATE_LIMIT: "100/minute"
  POOL_WORKER_COUNT: "2"
  HEARTBEAT_TIMEOUT: "15.0"
  LOG_FILE: ""   # stdout → recogido por K8s
```

#### 3.2b Secrets

```yaml
# pilar3/k8s/blockchain/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: blockchain-secret
  namespace: blockchain
type: Opaque
stringData:
  RABBITMQ_URL: "amqp://admin:<password>@rabbitmq.infra.svc.cluster.local:5672/"
  REDIS_URL: "redis://redis.infra.svc.cluster.local:6379"
  AUTHORITY_PUBKEY: "<64-chars-ed25519-pubkey>"
```

⚠️ Este archivo no debe contener valores reales. Los valores se inyectan vía `kubectl create secret` o ExternalSecret + Secret Manager. El archivo es solo el template.

#### 3.2c NCT Deployment

```yaml
# pilar3/k8s/blockchain/nct-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nct
  namespace: blockchain
spec:
  replicas: 1  # singleton — solo un coordinador
  selector:
    matchLabels:
      app: nct
  template:
    metadata:
      labels:
        app: nct
    spec:
      serviceAccountName: blockchain  # Workload Identity para pull de imágenes
      containers:
      - name: nct
        image: us-central1-docker.pkg.dev/edutokens/edutokens-repo/nct:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef:
            name: blockchain-config
        - secretRef:
            name: blockchain-secret
        env:
        - name: PORT
          value: "8080"
        - name: POOL_ID
          value: "pool-a"
        - name: HEALTH_PORT
          value: "8090"
        resources:
          requests: { memory: "128Mi", cpu: "250m" }
          limits:   { memory: "256Mi", cpu: "500m" }
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 15
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 5
```

#### 3.2d NCT Service

```yaml
# pilar3/k8s/blockchain/nct-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: nct
  namespace: blockchain
spec:
  type: ClusterIP  # sin IP pública, accedido vía Ingress
  selector:
    app: nct
  ports:
  - port: 8080
    targetPort: 8080
```

#### 3.2e Pool Deployment

```yaml
# pilar3/k8s/blockchain/pool-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pool-a
  namespace: blockchain
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pool-a
  template:
    metadata:
      labels:
        app: pool-a
    spec:
      serviceAccountName: blockchain
      containers:
      - name: pool-a
        image: us-central1-docker.pkg.dev/edutokens/edutokens-repo/pool:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 8090
        envFrom:
        - configMapRef:
            name: blockchain-config
        - secretRef:
            name: blockchain-secret
        env:
        - name: POOL_ID
          value: "pool-a"
        - name: HEALTH_PORT
          value: "8090"
        resources:
          requests: { memory: "128Mi", cpu: "250m" }
          limits:   { memory: "256Mi", cpu: "500m" }
        livenessProbe:
          httpGet:
            path: /health
            port: 8090
          initialDelaySeconds: 15
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8090
          initialDelaySeconds: 10
          periodSeconds: 5
```

#### 3.2f Pool Service

```yaml
# pilar3/k8s/blockchain/pool-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: pool-a
  namespace: blockchain
spec:
  type: ClusterIP
  selector:
    app: pool-a
  ports:
  - port: 8090
    targetPort: 8090
```

### 3.3 Network Policies (zero-trust)

```yaml
# pilar3/k8s/network-policies.yaml
---
# Solo blockchain puede hablar con Redis
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: redis-ingress
  namespace: infra
spec:
  podSelector:
    matchLabels:
      app: redis
  policyTypes: [Ingress]
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: blockchain
    ports:
    - port: 6379
---
# Solo blockchain puede hablar con RabbitMQ (AMQP interno)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: rabbitmq-ingress
  namespace: infra
spec:
  podSelector:
    matchLabels:
      app: rabbitmq
  policyTypes: [Ingress]
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: blockchain
    ports:
    - port: 5672
  # El tráfico externo (AMQPS :5671) viene del LoadBalancer,
  # no pasa por NetworkPolicy
```

### 3.4 Ingress (HTTP/HTTPS)

```yaml
# pilar3/k8s/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: edutokens-ingress
  namespace: infra
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  tls:
  - hosts:
    - rabbitmq.edutokens.duckdns.org
    # Futuro:
    # - api.edutokens.duckdns.org
    # - app.edutokens.duckdns.org
    secretName: edutokens-tls
  rules:
  - host: rabbitmq.edutokens.duckdns.org
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: rabbitmq
            port:
              number: 15672
  # Futuro:
  # - host: api.edutokens.duckdns.org
  #   http:
  #     paths:
  #     - path: /
  #       pathType: Prefix
  #       backend:
  #         service:
  #           name: nct
  #           port:
  #             number: 8080
```

### Entregables Fase 3

| Archivo | Descripción |
|---|---|
| `pilar3/k8s/infra/namespace.yaml` | Namespace `infra` con label para NetworkPolicy |
| `pilar3/k8s/infra/redis-statefulset.yaml` | Redis con persistencia AOF |
| `pilar3/k8s/infra/redis-service.yaml` | ClusterIP para Redis |
| `pilar3/k8s/infra/rabbitmq-statefulset.yaml` | RabbitMQ con TLS montado |
| `pilar3/k8s/infra/rabbitmq-service.yaml` | LoadBalancer + IP estática |
| `pilar3/k8s/infra/rabbitmq-configmap.yaml` | rabbitmq.conf con AMQPS |
| `pilar3/k8s/infra/rabbitmq-certificate.yaml` | Certificado TLS vía cert-manager |
| `pilar3/k8s/blockchain/namespace.yaml` | Namespace `blockchain` con label |
| `pilar3/k8s/blockchain/configmap.yaml` | Variables de configuración |
| `pilar3/k8s/blockchain/secret.yaml` | Template de secretos (valores dummy) |
| `pilar3/k8s/blockchain/nct-deployment.yaml` | NCT singleton |
| `pilar3/k8s/blockchain/nct-service.yaml` | ClusterIP para NCT |
| `pilar3/k8s/blockchain/pool-deployment.yaml` | Pool-A |
| `pilar3/k8s/blockchain/pool-service.yaml` | ClusterIP para Pool |
| `pilar3/k8s/network-policies.yaml` | Reglas de zero-trust |
| `pilar3/k8s/ingress.yaml` | Ingress HTTPS con certificado Let's Encrypt |

---

## Fase 4 — Workers en el cluster GPU del profesor

**Objetivo:** Manifiestos para desplegar workers con GPU en el entorno del profesor, conectándose vía AMQPS a nuestro RabbitMQ.

### 4.1 Dockerfile GPU worker

```dockerfile
# pilar3/docker/worker-gpu.Dockerfile
FROM nvidia/cuda:12.8-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar el código Python de Pilar 2 (mismos módulos que el worker CPU)
COPY pilar2/shared/ shared/
COPY pilar2/broker/ broker/
COPY pilar2/miner/ miner/
COPY pilar2/worker/ worker/

# Copiar el binario CUDA compilado en Pilar 1
COPY pilar1/md5_bf_range/md5_range /app/md5_range

RUN pip3 install --no-cache-dir pika fastapi uvicorn cryptography

ENV PYTHONPATH=/app
ENV MINER_BINARY=/app/md5_range

CMD ["python3", "-m", "worker.worker"]
```

### 4.2 Variables de entorno para el worker GPU

| Variable | Valor | Nota |
|---|---|---|
| `RABBITMQ_URL` | `amqps://admin:<password>@rabbitmq.edutokens.duckdns.org:5671/` | TLS sobre internet |
| `MINER_BINARY` | `/app/md5_range` | Binario CUDA nativo |
| `POOL_ID` | `pool-a` | Se une a nuestro pool |
| `HEARTBEAT_INTERVAL` | `5` | Keep-alive cada 5s |
| `HEALTH_PORT` | `8081` | Health endpoint HTTP |

### 4.3 Worker Deployment

Desplegado en el cluster GPU por el profesor:

```yaml
# pilar3/k8s/workers/worker-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker-gpu
  namespace: workers
spec:
  replicas: 2
  selector:
    matchLabels:
      app: worker-gpu
  template:
    metadata:
      labels:
        app: worker-gpu
    spec:
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      containers:
      - name: worker-gpu
        image: <ARTIFACT_REGISTRY_URL>/worker-gpu:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 8081
        envFrom:
        - secretRef:
            name: worker-gpu-secret
        env:
        - name: POOL_ID
          value: "pool-a"
        - name: HEARTBEAT_INTERVAL
          value: "5"
        - name: HEALTH_PORT
          value: "8081"
        resources:
          limits:
            nvidia.com/gpu: 1
        livenessProbe:
          httpGet:
            path: /health
            port: 8081
          initialDelaySeconds: 30
          periodSeconds: 10
```

### 4.4 Verificación de conectividad AMQPS

Script para validar que el worker puede alcanzar RabbitMQ vía TLS desde el cluster GPU:

```bash
# pilar3/scripts/test-amqps.sh
#!/bin/bash
# Prueba conectividad AMQPS desde un pod worker
RABBITMQ_HOST="rabbitmq.edutokens.duckdns.org"
RABBITMQ_PORT="5671"

# Verificar que el certificado es válido
openssl s_client -connect "${RABBITMQ_HOST}:${RABBITMQ_PORT}" \
  -servername "${RABBITMQ_HOST}" </dev/null 2>/dev/null | \
  openssl x509 -noout -dates -subject

echo "AMQPS connectivity OK"
```

### Entregables Fase 4

| Archivo | Descripción |
|---|---|
| `pilar3/docker/worker-gpu.Dockerfile` | Imagen Docker con CUDA + binario md5_range |
| `pilar3/k8s/workers/worker-deployment.yaml` | Deployment para el cluster GPU |
| `pilar3/k8s/workers/worker-secret.yaml` | Template de secret con RABBITMQ_URL |
| `pilar3/scripts/test-amqps.sh` | Script de verificación de conectividad |

---

## Fase 5 — CI/CD con GitHub Actions

**Objetivo:** Automatizar build, push, deploy, y escaneo de secretos.

### 5.1 Build & Push pipeline

```yaml
# .github/workflows/build-push.yml
name: Build and Push Docker Images

on:
  push:
    branches: [main]
    paths:
      - 'pilar2/**'
      - 'pilar1/md5_bf_range/**'
      - 'pilar3/docker/**'
      - '.github/workflows/build-push.yml'

jobs:
  build-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write   # OIDC para Workload Identity
    strategy:
      matrix:
        image: [nct, pool, worker, worker-gpu]
    steps:
      - uses: actions/checkout@v4

      - id: auth
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/XXX/locations/global/workloadIdentityPools/github-pool/providers/github-provider
          service_account: github-actions@edutokens.iam.gserviceaccount.com

      - uses: google-github-actions/setup-gcloud@v2

      - run: gcloud auth configure-docker us-central1-docker.pkg.dev

      - name: Build
        run: |
          if [ "${{ matrix.image }}" = "worker-gpu" ]; then
            docker build -f pilar3/docker/worker-gpu.Dockerfile \
              -t us-central1-docker.pkg.dev/edutokens/edutokens-repo/worker-gpu:${{ github.sha }} .
          else
            docker build -f pilar2/${{ matrix.image }}/Dockerfile \
              -t us-central1-docker.pkg.dev/edutokens/edutokens-repo/${{ matrix.image }}:${{ github.sha }} \
              pilar2/
          fi

      - name: Push
        run: |
          REPO="us-central1-docker.pkg.dev/edutokens/edutokens-repo/${{ matrix.image }}"
          docker tag ${REPO}:${{ github.sha }} ${REPO}:latest
          docker push ${REPO}:${{ github.sha }}
          docker push ${REPO}:latest
```

### 5.2 Deploy pipeline

```yaml
# .github/workflows/deploy-gke.yml
name: Deploy to GKE

on:
  push:
    branches: [main]
    paths:
      - 'pilar3/k8s/**'
      - '.github/workflows/deploy-gke.yml'

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/XXX/locations/global/workloadIdentityPools/github-pool/providers/github-provider
          service_account: github-actions@edutokens.iam.gserviceaccount.com

      - uses: google-github-actions/get-gke-credentials@v2
        with:
          cluster_name: edutokens-cluster
          location: us-central1

      - name: Apply K8s manifests
        run: |
          kubectl apply -f pilar3/k8s/infra/namespace.yaml
          kubectl apply -f pilar3/k8s/blockchain/namespace.yaml
          kubectl apply -f pilar3/k8s/infra/
          kubectl apply -f pilar3/k8s/blockchain/
          kubectl apply -f pilar3/k8s/network-policies.yaml
          kubectl apply -f pilar3/k8s/ingress.yaml

      - name: Verify rollout
        run: |
          kubectl -n blockchain rollout status deployment/nct --timeout=120s
          kubectl -n blockchain rollout status deployment/pool-a --timeout=120s
          kubectl -n infra rollout status statefulset/redis --timeout=120s
          kubectl -n infra rollout status statefulset/rabbitmq --timeout=120s

      - name: Smoke test
        run: |
          # Port-forward al NCT y verificar health
          kubectl -n blockchain port-forward svc/nct 8080:8080 &
          sleep 5
          curl -f http://localhost:8080/health || exit 1
          curl -f http://localhost:8080/status || exit 1
          kill %1
```

### 5.3 gitleaks pipeline

```yaml
# .github/workflows/gitleaks.yml
name: gitleaks scan

on:
  push:
    branches: [main, '**']
  pull_request:
    branches: [main]

jobs:
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: gitleaks/gitleaks-action@v2
        env:
          GITLEAKS_CONFIG: .gitleaks.toml
```

### 5.4 IaC pipeline (OpenTofu)

```yaml
# .github/workflows/tofu-plan.yml (PR)
name: OpenTofu Plan

on:
  pull_request:
    paths:
      - 'pilar3/tofu/**'

jobs:
  plan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_version: 1.9.0
      - run: tofu init
        working-directory: pilar3/tofu
      - run: tofu plan
        working-directory: pilar3/tofu
```

```yaml
# .github/workflows/tofu-apply.yml (main)
name: OpenTofu Apply

on:
  push:
    branches: [main]
    paths:
      - 'pilar3/tofu/**'

jobs:
  apply:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: opentofu/setup-opentofu@v1
      - run: tofu init
        working-directory: pilar3/tofu
      - run: tofu apply -auto-approve
        working-directory: pilar3/tofu
```

### Entregables Fase 5

| Archivo | Descripción |
|---|---|
| `.github/workflows/build-push.yml` | Build y push de imágenes Docker |
| `.github/workflows/deploy-gke.yml` | Deploy de manifiestos K8s + smoke test |
| `.github/workflows/gitleaks.yml` | Escaneo de secretos en cada push/PR |
| `.github/workflows/tofu-plan.yml` | Plan de infraestructura en PRs |
| `.github/workflows/tofu-apply.yml` | Aplicación de infraestructura en main |
| `.gitleaks.toml` | Configuración de reglas de detección |

---

## Fase 6 — Observabilidad (diferida)

**Objetivo:** Agregar Prometheus + Grafana. Diferida hasta que Fases 1-5 estén funcionando.

### 6.1 Métricas en servicios Python

Agregar `prometheus_client` como dependencia en los 3 Dockerfiles (nct, pool, worker).
Exponer `GET /metrics` con:

- `transactions_received_total` (counter)
- `blocks_mined_total` (counter)
- `mining_tasks_processed_total` (counter, por worker)
- `chain_height` (gauge)
- `active_workers` (gauge)
- `pool_active_pools` (gauge)
- `mining_latency_seconds` (histogram)

### 6.2 Prometheus

- Desplegar `kube-prometheus-stack` vía Helm
- Configurar `ServiceMonitor` para scrape de `/metrics` en pods con label `metrics: enabled`
- Alertmanager para notificar si `chain_height` deja de crecer o `active_workers == 0`

### 6.3 Grafana

- Dashboard con paneles: throughput de transacciones, bloques minados por minuto, workers activos, latencia P50/P95/P99, uso de CPU/memoria por pod
- Importar desde JSON para versionado

### 6.4 Logs estructurados

- Agregar `python-json-logger` a los servicios
- Formato JSON para que Cloud Logging los indexe automáticamente
- Campos: `service`, `block_index`, `task_id`, `worker_id`, `tx_id`

---

## Fase 7 — Verificación y documentación

**Objetivo:** Validar que todo funciona end-to-end y generar la documentación para la entrega.

### 7.1 Smoke tests end-to-end

1. Verificar que los 4 servicios responden health checks:
   ```bash
   kubectl -n infra port-forward svc/rabbitmq 15672:15672 &
   kubectl -n blockchain port-forward svc/nct 8080:8080 &
   curl localhost:8080/health
   curl localhost:8080/status
   ```

2. Enviar una transacción de prueba (requiere tener una keypair Ed25519):
   ```bash
   # Generar keypair con Python
   python3 -c "
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   sk = Ed25519PrivateKey.generate()
   print('PRIVATE:', sk.private_bytes_raw().hex())
   print('PUBLIC:', sk.public_key().public_bytes_raw().hex())
   "
   # Firmar y enviar
   curl -X POST http://localhost:8080/transaction \
     -H 'Content-Type: application/json' \
     -d '{"sender_pubkey":"...","receiver_pubkey":"...","amount":100,...}'
   ```

3. Esperar a que se mine un bloque (monitorear `GET /status`)

4. Verificar que la transacción aparece en la cadena:
   ```bash
   curl localhost:8080/chain | python3 -m json.tool | head -50
   ```

### 7.2 Diagrama de arquitectura cloud

- Actualizar `project_overview.md` con el diagrama de arquitectura GKE (el de este documento, sección "Arquitectura objetivo")
- Generar PNG para el informe (ej: con Mermaid CLI o Excalidraw)

### 7.3 pilar3/README.md

Estructura:
1. **Instrucciones de despliegue**:
   - Requisitos previos (gcloud, tofu, kubectl, dominio DuckDNS)
   - `tofu init && tofu apply` para crear el cluster
   - `kubectl apply -f pilar3/k8s/` para desplegar servicios
2. **Decisiones de diseño**:
   - Por qué LoadBalancer solo para RabbitMQ, no para NCT
   - Por qué AMQPS con Let's Encrypt en lugar de VPN
   - Por qué Workload Identity en lugar de imagePullSecrets
   - Por qué NCT es singleton (coordinador único)
3. **Variables de entorno** (tabla completa como la de `pilar2/.env.example` pero con valores cloud)
4. **Troubleshooting**: problemas comunes y soluciones

### 7.4 Informe final

- Integrar todo en el informe requerido por la consigna
- Sección de Pilar 3: arquitectura cloud, decisiones, métricas de despliegue
- Incluir screenshots de GKE Console, RabbitMQ Management, Grafana (si se implementó)

### Entregables Fase 7

| Archivo | Descripción |
|---|---|
| `pilar3/README.md` | Documentación completa del despliegue cloud |
| `pilar3/scripts/smoke-test.sh` | Script de verificación end-to-end |
| `project_overview.md` (actualizado) | Agregar sección de arquitectura cloud |
| Informe final (Google Docs / PDF) | Fuera de este plan, gestionado por el equipo |

---

## Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Workers no validan certificado TLS de RabbitMQ | Media | Alto — sin minería no hay blockchain | pika usa el CA bundle del sistema. Si el worker corre en Ubuntu, Let's Encrypt está en el trust store. Si no, configurar `ssl_options` con path al CA cert. |
| IPs de salida del cluster GPU no predecibles | Media | Medio — firewall no se puede configurar | Pedir el rango al profesor. Alternativa: autenticación por certificado de cliente (mTLS) además del TLS del servidor. |
| Rate limits de GCP Free Trial | Baja | Bajo — el cluster puede no crearse | Verificar cuota antes de `tofu apply`. Usar `e2-small` para observabilidad. |
| `md5_range` compilado para sm_75 no corre en la GPU del profesor | Alta | Alto — workers sin minería real | Compilar para la arquitectura correcta una vez conocido el modelo (`nvcc -arch=sm_XX`). Tener el CPU fallback como plan B. |
| cert-manager no puede validar dominio DuckDNS | Baja | Medio | Usar DNS-01 challenge si HTTP-01 falla (DuckDNS soporta TXT records vía API). Alternativa: certificado autofirmado con CA distribuido. |
| Tiempo insuficiente para todas las fases | Alta | Medio | Priorizar Fases 1→2→3→7. Diferir Fase 4, 5 parcial, y Fase 6. Con Fase 3 ya tenemos el sistema corriendo en GKE internamente. |

---

## Orden de ejecución

```
Día 1-2:  Fase 1 (Artifact Registry) + Fase 2 (OpenTofu)
          └── Al final del día 2: cluster GKE creado, imágenes en registry

Día 2-3:  Fase 3 (K8s Manifests)
          └── Al final del día 3: NCT + Pool + Redis + RabbitMQ corriendo en GKE

Día 3:    Fase 4 (Workers GPU) — en paralelo con ajustes de Fase 3
          └── Al final del día 3: workers minando desde el cluster del profesor

Día 3-4:  Fase 5 (CI/CD) + Fase 7 (Verificación y documentación)
          └── Al final del día 4: pipelines funcionando, smoke tests pasando

Día 4+:   Fase 6 (Observabilidad) — diferida, solo si hay margen
```

---

## Archivos nuevos totales

| Archivo | Fase |
|---|---|
| `pilar3/.artifacts/cloud-migration-plan.md` | (este documento) |
| `pilar3/tofu/versions.tf` | 2 |
| `pilar3/tofu/main.tf` | 2 |
| `pilar3/tofu/variables.tf` | 2 |
| `pilar3/tofu/outputs.tf` | 2 |
| `pilar3/tofu/vpc.tf` | 2 |
| `pilar3/tofu/gke.tf` | 2 |
| `pilar3/tofu/cert-manager.tf` | 2 |
| `pilar3/tofu/artifact-registry.tf` | 1 |
| `pilar3/tofu/iam.tf` | 1 |
| `pilar3/tofu/firewall.tf` | 2 |
| `pilar3/tofu/terraform.tfvars.example` | 2 |
| `pilar3/k8s/infra/namespace.yaml` | 3 |
| `pilar3/k8s/infra/redis-statefulset.yaml` | 3 |
| `pilar3/k8s/infra/redis-service.yaml` | 3 |
| `pilar3/k8s/infra/rabbitmq-statefulset.yaml` | 3 |
| `pilar3/k8s/infra/rabbitmq-service.yaml` | 3 |
| `pilar3/k8s/infra/rabbitmq-configmap.yaml` | 3 |
| `pilar3/k8s/infra/rabbitmq-certificate.yaml` | 3 |
| `pilar3/k8s/blockchain/namespace.yaml` | 3 |
| `pilar3/k8s/blockchain/configmap.yaml` | 3 |
| `pilar3/k8s/blockchain/secret.yaml` | 3 |
| `pilar3/k8s/blockchain/nct-deployment.yaml` | 3 |
| `pilar3/k8s/blockchain/nct-service.yaml` | 3 |
| `pilar3/k8s/blockchain/pool-deployment.yaml` | 3 |
| `pilar3/k8s/blockchain/pool-service.yaml` | 3 |
| `pilar3/k8s/network-policies.yaml` | 3 |
| `pilar3/k8s/ingress.yaml` | 3 |
| `pilar3/docker/worker-gpu.Dockerfile` | 4 |
| `pilar3/k8s/workers/worker-deployment.yaml` | 4 |
| `pilar3/k8s/workers/worker-secret.yaml` | 4 |
| `pilar3/scripts/test-amqps.sh` | 4 |
| `.github/workflows/build-push.yml` | 5 |
| `.github/workflows/deploy-gke.yml` | 5 |
| `.github/workflows/gitleaks.yml` | 5 |
| `.github/workflows/tofu-plan.yml` | 5 |
| `.github/workflows/tofu-apply.yml` | 5 |
| `.gitleaks.toml` | 5 |
| `pilar3/README.md` | 7 |
| `pilar3/scripts/smoke-test.sh` | 7 |

**Total: 37 archivos nuevos.**

---

## Decisiones de diseño (registro)

| # | Decisión | Justificación |
|---|---|---|
| D1 | **Mismo repositorio** para infra y código | Consigna pide un único repo público. Imágenes Docker y K8s manifests están acoplados al código fuente. |
| D2 | **AMQPS (TLS) en RabbitMQ**, no VPN | Los workers están en otro cluster administrativo. AMQPS con Let's Encrypt es más simple que configurar una VPN site-to-site. |
| D3 | **LoadBalancer solo en RabbitMQ**, NCT/Pool con ClusterIP | RabbitMQ necesita ser alcanzable desde fuera del cluster (workers GPU). NCT es accedido vía Ingress desde el frontend. Menor superficie de ataque. |
| D4 | **cert-manager + Let's Encrypt** para todos los certificados | Automatiza la renovación. Usa el mismo ClusterIssuer para Ingress (HTTP) y para RabbitMQ AMQPS (montado en pod). |
| D5 | **NCT singleton** (replicas: 1) | Solo debe haber un coordinador decidiendo qué transacciones entran en cada bloque. En el futuro se puede hacer leader election, pero para el TP es overkill. |
| D6 | **Workload Identity** para GCR y GCP APIs | Zero static keys. Los pods se autentican automáticamente. Cumple con el requisito de la consigna. |
| D7 | **OpenTofu sobre Terraform** | La consigna menciona OpenTofu explícitamente. Es un drop-in replacement de Terraform, misma sintaxis HCL. |
| D8 | **Redis como StatefulSet con PVC** | AOF persistence requiere volumen persistente. StatefulSet garantiza identidad estable y volumen dedicado. |
| D9 | **NetworkPolicy zero-trust** entre namespaces | `infra` solo acepta tráfico de `blockchain`. `blockchain` solo acepta de `apps`. Minimiza el blast radius si un namespace es comprometido. |
| D10 | **Imágenes con tag `:$GIT_SHA` + `:latest`** | `:$GIT_SHA` para rollback determinístico. `:latest` para conveniencia en desarrollo. `imagePullPolicy: Always` asegura que `:latest` siempre se refresca. |
