# Outputs — valores necesarios para CI/CD y comandos post-tofu

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

output "rabbitmq_static_ip" {
  description = "IP pública estática reservada para RabbitMQ LoadBalancer"
  value       = google_compute_address.rabbitmq.address
}

output "rabbitmq_static_ip_name" {
  description = "Nombre del recurso de IP estática (para anotaciones en el Service)"
  value       = google_compute_address.rabbitmq.name
}

output "nginx_ingress_static_ip" {
  description = "IP pública estática para nginx-ingress LoadBalancer"
  value       = google_compute_address.nginx_ingress.address
}

output "nginx_ingress_static_ip_name" {
  description = "Nombre del recurso de IP estática del nginx-ingress"
  value       = google_compute_address.nginx_ingress.name
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

output "gke_pull_service_account" {
  description = "Email de la service account GCP para Workload Identity (usar en la anotación de KSA)"
  value       = google_service_account.gke_pull.email
}
