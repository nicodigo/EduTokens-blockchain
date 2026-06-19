# OpenTofu — Providers y versiones
# Solo Google Cloud. Kubernetes y Helm se manejan con kubectl (más simple, más claro).

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state (opcional para equipos)
  # backend "gcs" {
  #   bucket = "edutokens-tfstate"
  #   prefix = "terraform/state"
  # }
}
