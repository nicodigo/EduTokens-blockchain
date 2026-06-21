#!/usr/bin/env bash
# deploy-k8s.sh — Aplica los manifiestos Kubernetes en el orden correcto
# Uso: ./pilar3/scripts/deploy-k8s.sh
# Requiere: haber copiado secret.yaml.example → secret.yaml y completado valores

set -euo pipefail

K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

echo "==> 1/6 Namespaces"
kubectl apply -f "$K8S_DIR/infra/namespace.yaml"
kubectl apply -f "$K8S_DIR/blockchain/namespace.yaml"
kubectl apply -f "$K8S_DIR/apps/namespace.yaml"

echo "==> 2/6 ConfigMaps + Secrets + ServiceAccount + ClusterIssuer"
kubectl apply -f "$K8S_DIR/blockchain/configmap.yaml"
kubectl apply -f "$K8S_DIR/blockchain/secret.yaml"
kubectl apply -f "$K8S_DIR/blockchain/service-account.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-configmap.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-secret.yaml"
kubectl apply -f "$K8S_DIR/cert-manager/cluster-issuer.yaml"

# ── EduTokens App ConfigMaps + Secrets ─────────────────────────
kubectl apply -f "$K8S_DIR/apps/postgres-init-configmap.yaml"
kubectl apply -f "$K8S_DIR/apps/postgres-secret.yaml"
kubectl apply -f "$K8S_DIR/apps/backend-configmap.yaml"
kubectl apply -f "$K8S_DIR/apps/backend-secret.yaml"
kubectl apply -f "$K8S_DIR/apps/frontend-configmap.yaml"

echo "==> 3/6 Services"
kubectl apply -f "$K8S_DIR/infra/redis-service.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-service.yaml"

# ── EduTokens App Services ──────────────────────────────────────
kubectl apply -f "$K8S_DIR/apps/postgres-service.yaml"
kubectl apply -f "$K8S_DIR/apps/backend-service.yaml"
kubectl apply -f "$K8S_DIR/apps/frontend-service.yaml"

echo "==> 4/6 StatefulSets + Deployments (blockchain)"
kubectl apply -f "$K8S_DIR/infra/redis-statefulset.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-statefulset.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-deployment.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-deployment.yaml"

echo "==> 5/6 StatefulSets + Deployments (app)"
# PostgreSQL primero (el backend depende de él)
kubectl apply -f "$K8S_DIR/apps/postgres-statefulset.yaml"

# Esperar a que PostgreSQL esté listo antes de desplegar el backend
echo "   Esperando a que PostgreSQL esté listo..."
kubectl -n apps wait --for=condition=ready pod -l app=postgres --timeout=120s 2>/dev/null || true

kubectl apply -f "$K8S_DIR/apps/backend-deployment.yaml"
kubectl apply -f "$K8S_DIR/apps/frontend-deployment.yaml"

echo "==> 6/6 Ingress"
# El Ingress está en apps (único namespace con tráfico HTTPS)
kubectl apply -f "$K8S_DIR/ingress.yaml"

echo ""
echo "==> Esperando a que todos los pods estén listos..."
kubectl -n infra      wait --for=condition=ready pod -l app=redis    --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=nct      --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=pool-a   --timeout=120s 2>/dev/null || true
kubectl -n apps       wait --for=condition=ready pod -l app=postgres --timeout=120s 2>/dev/null || true
kubectl -n apps       wait --for=condition=ready pod -l app=backend  --timeout=120s 2>/dev/null || true
kubectl -n apps       wait --for=condition=ready pod -l app=frontend --timeout=120s 2>/dev/null || true
# RabbitMQ depende del certificado — puede demorar más
kubectl -n infra      wait --for=condition=ready pod -l app=rabbitmq --timeout=300s 2>/dev/null || true

echo ""
echo "==> Estado final"
kubectl get pods -n infra
kubectl get pods -n blockchain
kubectl get pods -n apps
kubectl get certificate -n apps

echo ""
echo "✅ Deploy completado"

# ── Verificación rápida ─────────────────────────────────────────
echo ""
echo "==> Verificación rápida de endpoints"
echo "NCT health:"
kubectl -n blockchain exec deploy/nct -- curl -sf http://localhost:8080/health 2>/dev/null || echo "  ⚠️  NCT aún no responde"
echo ""
echo "Backend health:"
kubectl -n apps exec deploy/backend -- curl -sf http://localhost:8000/health 2>/dev/null || echo "  ⚠️  Backend aún no responde"
echo ""
echo "Frontend health (via nginx):"
kubectl -n apps exec deploy/frontend -- curl -sf http://localhost:80/health 2>/dev/null || echo "  ⚠️  Frontend aún no listo"
