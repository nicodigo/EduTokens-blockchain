# Firewall rules — defensa en profundidad
# GKE auto-crea reglas para LoadBalancer y node ports.
# Estas reglas agregan capas adicionales de restricción.

# Permitir AMQPS (puerto 5671) desde el cluster GPU del profesor
# Los workers externos se conectan vía LoadBalancer, pero esta regla
# agrega una capa de red adicional como defense-in-depth.
resource "google_compute_firewall" "allow_amqps_from_gpu" {
  name    = "allow-amqps-from-gpu"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["5671"]
  }

  source_ranges = length(var.gpu_source_ranges) > 0 ? var.gpu_source_ranges : ["0.0.0.0/0"]
  target_tags   = ["gke-node"]  # GKE auto-tags nodes with gke-<cluster-name>-<hash>-node

  description = "AMQPS desde el cluster GPU del profesor (actualizar source_ranges cuando se conozcan las IPs)"

  depends_on = [google_project_service.services]
}

# Permitir SSH vía IAP (Identity-Aware Proxy)
# Útil para troubleshooting de nodos GKE
resource "google_compute_firewall" "allow_ssh_iap" {
  name    = "allow-ssh-iap"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.ssh_source_ranges   # default: IAP range
  target_tags   = ["gke-node"]

  description = "SSH vía IAP"
}
