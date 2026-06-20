#!/usr/bin/env bash
# deploy-k8s.sh — Aplica los manifiestos Kubernetes en el orden correcto
# Uso: ./pilar3/scripts/deploy-k8s.sh
# Requiere: haber copiado secret.yaml.example → secret.yaml y completado valores

set -euo pipefail

K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

echo "==> 1/5 Namespaces"
kubectl apply -f "$K8S_DIR/infra/namespace.yaml"
kubectl apply -f "$K8S_DIR/blockchain/namespace.yaml"
kubectl apply -f "$K8S_DIR/apps/namespace.yaml"

echo "==> 2/5 ConfigMaps + Secrets + ServiceAccount + ClusterIssuer"
kubectl apply -f "$K8S_DIR/blockchain/configmap.yaml"
kubectl apply -f "$K8S_DIR/blockchain/secret.yaml"
kubectl apply -f "$K8S_DIR/blockchain/service-account.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-configmap.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-secret.yaml"
kubectl apply -f "$K8S_DIR/cert-manager/cluster-issuer.yaml"

echo "==> 3/5 Services"
kubectl apply -f "$K8S_DIR/infra/redis-service.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-service.yaml"

echo "==> 4/5 StatefulSets + Deployments"
kubectl apply -f "$K8S_DIR/infra/redis-statefulset.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-statefulset.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-deployment.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-deployment.yaml"

echo "==> 5/5 Ingress"
# El Ingress está en apps (único namespace con tráfico HTTPS)
kubectl apply -f "$K8S_DIR/ingress.yaml"

echo ""
echo "==> Esperando a que todos los pods estén listos..."
kubectl -n infra      wait --for=condition=ready pod -l app=redis    --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=nct      --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=pool-a   --timeout=120s 2>/dev/null || true
# RabbitMQ depende del certificado — puede demorar más
kubectl -n infra      wait --for=condition=ready pod -l app=rabbitmq --timeout=300s 2>/dev/null || true

echo ""
echo "==> Estado final"
kubectl get pods -n infra
kubectl get pods -n blockchain
kubectl get certificate -n apps

echo ""
echo "✅ Deploy completado"
