# Provider de Google — autenticación vía ADC (gcloud auth application-default login)
provider "google" {
  project = var.project_id
  region  = var.region
}

# ── NOTA ───────────────────────────────────────────────────────────
# Kubernetes y Helm NO se manejan desde OpenTofu.
# Después de tofu apply, ejecutar los pasos manuales detallados en
# pilar3/README.md o en la sección "Post-tofu setup" de este archivo.
# Esto evita el chicken-and-egg problem del provider de Kubernetes.
# ───────────────────────────────────────────────────────────────────
