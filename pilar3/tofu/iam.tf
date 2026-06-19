# IAM — Workload Identity para GKE → Artifact Registry
# Los pods en GKE se autentican sin credenciales hardcodeadas.

# Service account de GCP para pull de imágenes
resource "google_service_account" "gke_pull" {
  account_id   = "gke-pull-images"
  display_name = "GKE Artifact Registry Puller"
  project      = var.project_id

  depends_on = [google_project_service.services]  # garantiza iam.googleapis.com activa
}

# Permiso de lectura sobre el repositorio Docker
resource "google_artifact_registry_repository_iam_member" "reader" {
  location   = var.region
  repository = google_artifact_registry_repository.docker.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gke_pull.email}"
}

# Workload Identity: mapea la KSA "blockchain/blockchain" a la GSA "gke-pull-images"
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.gke_pull.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[blockchain/blockchain]"
}

# Service account de GCP para GitHub Actions (OIDC)
resource "google_service_account" "github_actions" {
  account_id   = "github-actions"
  display_name = "GitHub Actions CI/CD"
  project      = var.project_id

  depends_on = [google_project_service.services]  # garantiza iam.googleapis.com activa
}

# Permiso para pushear imágenes desde CI
resource "google_artifact_registry_repository_iam_member" "writer" {
  location   = var.region
  repository = google_artifact_registry_repository.docker.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_actions.email}"
}

# Permiso para desplegar en GKE desde CI
resource "google_project_iam_member" "github_actions_gke" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}
