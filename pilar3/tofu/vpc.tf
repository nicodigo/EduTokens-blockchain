# VPC, subred, Cloud NAT y Cloud Router
# Free tier: tráfico outbound vía NAT (más barato que IPs externas por nodo)

# APIs mínimas necesarias
resource "google_project_service" "services" {
  for_each = toset([
    "container.googleapis.com",          # GKE
    "compute.googleapis.com",             # VPC, firewalls, IP estática
    "artifactregistry.googleapis.com",    # Docker images
    "iam.googleapis.com",                 # Workload Identity
    "cloudresourcemanager.googleapis.com",# IAM bindings
  ])

  project = var.project_id
  service = each.key

  disable_on_destroy = false
}

# --- VPC ---
resource "google_compute_network" "vpc" {
  name                    = "edutokens-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "edutokens-subnet"
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = "10.0.0.0/20"

  # Rangos secundarios para GKE (VPC-native)
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.1.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.2.0.0/20"
  }

  private_ip_google_access = true
}

# --- Cloud NAT (salida a internet para pods sin IP externa) ---
resource "google_compute_router" "router" {
  name    = "edutokens-router"
  region  = var.region
  network = google_compute_network.vpc.name
}

resource "google_compute_router_nat" "nat" {
  name                               = "edutokens-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# --- IP estática regional para RabbitMQ LoadBalancer ---
resource "google_compute_address" "rabbitmq" {
  name         = "rabbitmq-static-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "STANDARD"  # free tier: STANDARD tier es más barato

  depends_on = [google_project_service.services]  # garantiza compute.googleapis.com activa
}
