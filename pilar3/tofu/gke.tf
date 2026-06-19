# GKE Cluster — zonal, VPC-native, Workload Identity, Network Policy
# Free tier: cluster zonal = sin costo de plano de control
# Cuota: 8 vCPUs en us-central1 → 2 nodos e2-standard-2 = 4 vCPUs

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.zone

  # Eliminar el node pool por defecto y crear uno personalizado
  remove_default_node_pool = true
  initial_node_count       = 1

  network    = google_compute_network.vpc.name
  subnetwork = google_compute_subnetwork.subnet.name

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Workload Identity — pods se autentican contra GCP sin keys
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Network Policy (Calico) — necesario para zero-trust entre namespaces
  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  # Canal de release REGULAR — más estable que RAPID
  release_channel {
    channel = "REGULAR"
  }

  # Free tier: sin maintenance exclusion window (no disponible en free tier)
  maintenance_policy {
    recurring_window {
      start_time = "2025-01-01T04:00:00Z"
      end_time   = "2025-01-01T08:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SU"
    }
  }

  # Deshabilitar servicios no necesarios para ahorrar recursos
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  deletion_protection = false  # free tier: permitir destrucción fácil

  depends_on = [google_project_service.services]
}

# --- Node Pool: infra-apps ---
# Corre Redis, RabbitMQ, NCT, Pool
resource "google_container_node_pool" "infra_apps" {
  name     = "infra-apps"
  location = var.zone
  cluster  = google_container_cluster.primary.name

  node_count = var.node_count

  node_config {
    machine_type = var.node_machine_type
    disk_size_gb = var.node_disk_size_gb
    disk_type    = "pd-standard"   # HDD: más barato, suficiente para servicios stateless + Redis AOF

    image_type = "COS_CONTAINERD"

    # OAuth scopes mínimos necesarios
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    labels = {
      role = "infra-apps"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
}

# --- Kubernetes Service Accounts para Workload Identity ---
# KSA "blockchain" en namespace "blockchain"
resource "kubernetes_service_account" "blockchain" {
  metadata {
    name      = "blockchain"
    namespace = "blockchain"
    annotations = {
      "iam.gke.io/gcp-service-account" = google_service_account.gke_pull.email
    }
  }

  depends_on = [google_container_node_pool.infra_apps]
}
