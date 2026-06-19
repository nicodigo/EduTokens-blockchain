# Artifact Registry — repositorio Docker
# Free tier: 0.5 GB storage gratis, después $0.10/GB/mes

resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  repository_id = "edutokens-repo"
  description   = "Imágenes Docker de EduTokens (nct, pool, worker, worker-gpu)"
  format        = "DOCKER"

  docker_config {
    immutable_tags = false  # permite re-taggear latest
  }

  depends_on = [google_project_service.services]
}
