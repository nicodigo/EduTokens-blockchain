# Variables de entrada

variable "project_id" {
  description = "ID del proyecto GCP"
  type        = string
  default     = "edutokens-2026"
}

variable "region" {
  description = "Región de GCP (us-central1 tiene 8 vCPUs en free tier)"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zona para el cluster GKE (zonal = sin costo de plano de control)"
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "Nombre del cluster GKE"
  type        = string
  default     = "edutokens-cluster"
}

variable "domain_name" {
  description = "Dominio raíz para certificados TLS (ej: edutokens.duckdns.org)"
  type        = string
}

variable "letsencrypt_email" {
  description = "Email para notificaciones de Let's Encrypt (expiración de certificados)"
  type        = string
}

variable "gpu_source_ranges" {
  description = "CIDRs del cluster GPU del profesor (para permitir AMQPS en firewall)"
  type        = list(string)
  default     = []
}

variable "ssh_source_ranges" {
  description = "CIDRs permitidos para SSH (IAP: 35.235.240.0/20)"
  type        = list(string)
  default     = ["35.235.240.0/20"]
}

# --- Free tier quotas ---
variable "node_machine_type" {
  description = "Tipo de máquina para el node pool de infra/apps"
  type        = string
  default     = "e2-standard-2"   # 2 vCPU × 2 nodos = 4 vCPUs (cuota máxima: 8)
}

variable "node_count" {
  description = "Cantidad de nodos en el pool principal"
  type        = number
  default     = 2
}

variable "node_disk_size_gb" {
  description = "Tamaño de disco por nodo (GB)"
  type        = number
  default     = 30
}
