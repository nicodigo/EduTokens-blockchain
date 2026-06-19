# Provider de Google — autenticación vía ADC (gcloud auth application-default login)
provider "google" {
  project = var.project_id
  region  = var.region
}

# Datos del cliente para kubernetes y helm providers
data "google_client_config" "default" {}

# Provider de Kubernetes — apunta al cluster GKE una vez creado
provider "kubernetes" {
  host  = "https://${google_container_cluster.primary.endpoint}"
  token = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(
    google_container_cluster.primary.master_auth[0].cluster_ca_certificate
  )
}

# Provider de Helm — para instalar cert-manager y nginx-ingress
provider "helm" {
  kubernetes {
    host  = "https://${google_container_cluster.primary.endpoint}"
    token = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(
      google_container_cluster.primary.master_auth[0].cluster_ca_certificate
    )
  }
}
