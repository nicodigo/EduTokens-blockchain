# OpenTofu — Providers y versiones
# Free tier GCP: 8 vCPUs en us-central1, PD standard, cluster zonal

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state (opcional para equipos)
  # backend "gcs" {
  #   bucket = "edutokens-tfstate"
  #   prefix = "terraform/state"
  # }
}
