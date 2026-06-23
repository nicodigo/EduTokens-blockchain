#!/usr/bin/env bash
# deploy-k8s.sh — Aplica los manifiestos Kubernetes de blockchain e infra
# Uso: ./pilar3/scripts/deploy-k8s.sh
# Requiere: haber copiado secret.yaml.example → secret.yaml y completado valores
#
# NOTA: Los manifests del namespace apps se movieron a EduTokens-app/k8s/
#       Usar EduTokens-app/scripts/deploy.sh para desplegar el app namespace.

set -euo pipefail

K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

echo "==> 1/4 Namespaces"
kubectl apply -f "$K8S_DIR/infra/namespace.yaml"
kubectl apply -f "$K8S_DIR/blockchain/namespace.yaml"

echo "==> 2/4 ConfigMaps + Secrets + ServiceAccount"
kubectl apply -f "$K8S_DIR/blockchain/configmap.yaml"
kubectl apply -f "$K8S_DIR/blockchain/secret.yaml"
kubectl apply -f "$K8S_DIR/blockchain/service-account.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-configmap.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-secret.yaml"

echo "==> 3/4 Services"
kubectl apply -f "$K8S_DIR/infra/redis-service.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-service.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-service.yaml"

echo "==> 4/5 StatefulSets + Deployments"
kubectl apply -f "$K8S_DIR/infra/redis-statefulset.yaml"
kubectl apply -f "$K8S_DIR/infra/rabbitmq-statefulset.yaml"
kubectl apply -f "$K8S_DIR/blockchain/nct-deployment.yaml"
kubectl apply -f "$K8S_DIR/blockchain/pool-deployment.yaml"

echo ""
echo "==> Esperando a que los pods de blockchain estén listos..."
kubectl -n infra      wait --for=condition=ready pod -l app=redis    --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=nct      --timeout=120s 2>/dev/null || true
kubectl -n blockchain wait --for=condition=ready pod -l app=pool-a   --timeout=120s 2>/dev/null || true
kubectl -n infra      wait --for=condition=ready pod -l app=rabbitmq --timeout=300s 2>/dev/null || true

echo "==> 5/5 Ingress + Monitoring"
# ExternalName Services — proxy en apps → cross-namespace
kubectl apply -f "$K8S_DIR/nct-external-service.yaml"
kubectl apply -f "$K8S_DIR/monitoring/grafana-external-service.yaml"

# Ingress — requiere que el namespace apps ya exista
kubectl apply -f "$K8S_DIR/ingress.yaml"

# Observability stack (Prometheus + Grafana)
kubectl apply -f "$K8S_DIR/monitoring/namespace.yaml"
kubectl apply -f "$K8S_DIR/monitoring/prometheus-configmap.yaml"
kubectl apply -f "$K8S_DIR/monitoring/prometheus-deployment.yaml"
kubectl apply -f "$K8S_DIR/monitoring/grafana-datasource-configmap.yaml"
kubectl apply -f "$K8S_DIR/monitoring/grafana-dashboard-configmap.yaml"
kubectl apply -f "$K8S_DIR/monitoring/grafana-deployment.yaml"

kubectl -n monitoring wait --for=condition=ready pod -l app=prometheus --timeout=120s 2>/dev/null || true
kubectl -n monitoring wait --for=condition=ready pod -l app=grafana    --timeout=120s 2>/dev/null || true

echo ""
echo "==> Estado final"
kubectl get pods -n infra
kubectl get pods -n blockchain
kubectl get pods -n monitoring

echo ""
echo "✅ Deploy de blockchain completado"

# ── Verificación rápida ─────────────────────────────────────────
echo ""
echo "==> Verificación rápida de endpoints"
echo "NCT health:"
kubectl -n blockchain exec deploy/nct -- curl -sf http://localhost:8080/health 2>/dev/null || echo "  ⚠️  NCT aún no responde"
echo "Prometheus targets:"
kubectl -n monitoring exec deploy/prometheus -- wget -qO- http://localhost:9090/api/v1/targets 2>/dev/null | python3 -c "
import json,sys; d=json.load(sys.stdin)
for t in d.get('data',{}).get('activeTargets',[]):
    print(f\"  {'✅' if t['health']=='up' else '❌'} {t['labels'].get('job','?')} — {t['scrapeUrl']}\")
" 2>/dev/null || echo "  ⚠️  Prometheus aún no listo"
echo ""
echo "Accesos públicos:"
echo "  App:       https://edutokens.xyz"
echo "  NCT API:   https://nct.edutokens.xyz/health"
echo "  Grafana:   https://grafana.edutokens.xyz (admin / admin)"