# ── Post-tofu: cert-manager y nginx-ingress ──────────────────────
# Estos componentes se instalan con kubectl después de que tofu
# haya creado el cluster GKE. Ver pilar3/README.md para el flujo completo.
#
# Resumen:
#   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
#   kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml
#   kubectl apply -f pilar3/k8s/cert-manager/
#   kubectl apply -f pilar3/k8s/
# ───────────────────────────────────────────────────────────────────
