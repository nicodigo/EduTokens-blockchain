# Outputs — valores necesarios para CI/CD y deploys manuales

output "project_id" {
  description = "Project ID"
  value       = var.project_id
}

output "region" {
  description = "Región del cluster"
  value       = var.region
}

output "zone" {
  description = "Zona del cluster"
  value       = var.zone
}

output "cluster_name" {
  description = "Nombre del cluster GKE"
  value       = google_container_cluster.primary.name
}

output "cluster_endpoint" {
  description = "Endpoint del plano de control de GKE"
  value       = google_container_cluster.primary.endpoint
  sensitive   = true
}

output "cluster_ca_certificate" {
  description = "CA certificate del cluster (base64)"
  value       = google_container_cluster.primary.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

output "rabbitmq_static_ip" {
  description = "IP pública estática reservada para RabbitMQ LoadBalancer"
  value       = google_compute_address.rabbitmq.address
}

output "rabbitmq_static_ip_name" {
  description = "Nombre del recurso de IP estática (para anotaciones en el Service)"
  value       = google_compute_address.rabbitmq.name
}

output "artifact_registry_url" {
  description = "URL del repositorio Docker en Artifact Registry"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker.repository_id}"
}

output "vpc_name" {
  description = "Nombre de la VPC"
  value       = google_compute_network.vpc.name
}

output "subnet_name" {
  description = "Nombre de la subred"
  value       = google_compute_subnetwork.subnet.name
}

output "get_credentials_command" {
  description = "Comando para obtener credenciales kubectl"
  value       = "gcloud container clusters get-credentials ${var.cluster_name} --zone ${var.zone} --project ${var.project_id}"
}
