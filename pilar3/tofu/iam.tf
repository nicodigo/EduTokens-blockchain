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

# ────────────────────────────────────────────────────────────────────
# Workload Identity Federation — GitHub Actions → GCP (OIDC)
# Permite que GitHub Actions se autentique sin service account keys.
# ────────────────────────────────────────────────────────────────────

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_iam_workload_identity_pool" "github" {
  provider                  = google
  workload_identity_pool_id = "github-actions-oidc"
  display_name              = "GitHub Actions OIDC"
  description               = "Pool para federación OIDC con GitHub Actions — repo nicodigo/EduTokens-blockchain"
  project                   = var.project_id
  disabled                  = false

  depends_on = [google_project_service.services]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  provider                           = google
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions-provider"
  display_name                       = "GitHub Actions OIDC Provider"
  description                        = "Proveedor OIDC para GitHub Actions — issuer token.actions.githubusercontent.com"
  project                            = var.project_id

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository.startsWith('nicodigo/')"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  depends_on = [google_iam_workload_identity_pool.github]
}

# Permite que los workflows de nuestro repo asuman la SA github-actions
resource "google_service_account_iam_member" "github_oidc" {
  service_account_id = google_service_account.github_actions.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/nicodigo/EduTokens-blockchain"
}
