#!/usr/bin/env bash
# build-push.sh — Build y push de imágenes Docker a Artifact Registry
# Uso: ./pilar3/scripts/build-push.sh
# Requiere: gcloud auth configure-docker us-central1-docker.pkg.dev ejecutado antes

set -euo pipefail

REGISTRY="us-central1-docker.pkg.dev/edutokens-2026/edutokens-repo"
# Nota: el project ID correcto es edutokens-2026 (con guion y año)
PILAR2="$(cd "$(dirname "$0")/../../pilar2" && pwd)"

echo "==> Autenticando contra Artifact Registry..."
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

echo ""
echo "==> Build NCT..."
docker build -f "$PILAR2/nct/Dockerfile"    -t "$REGISTRY/nct:latest" "$PILAR2"

echo "==> Build Pool..."
docker build -f "$PILAR2/pool/Dockerfile"   -t "$REGISTRY/pool:latest" "$PILAR2"

echo "==> Build Worker CPU..."
docker build -f "$PILAR2/worker/Dockerfile" -t "$REGISTRY/worker-cpu:latest" "$PILAR2"

echo "==> Build Worker GPU..."
docker build \
  -f pilar3/docker/worker-gpu.Dockerfile \
  -t "$REGISTRY/worker-gpu:latest" \
  .

echo ""
echo "==> Push NCT..."
docker push "$REGISTRY/nct:latest"

echo "==> Push Pool..."
docker push "$REGISTRY/pool:latest"

echo "==> Push Worker CPU..."
docker push "$REGISTRY/worker-cpu:latest"

echo "==> Push Worker GPU..."
docker push "$REGISTRY/worker-gpu:latest"

echo ""
echo "✅ Build y push completado"
echo "   $REGISTRY/nct:latest"
echo "   $REGISTRY/pool:latest"
echo "   $REGISTRY/worker-cpu:latest"
echo "   $REGISTRY/worker-gpu:latest"
