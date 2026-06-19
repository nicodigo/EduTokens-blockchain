# terraform.tfvars — completar los campos marcados con ⚠️
# Dominio DuckDNS confirmado: edutokens.duckdns.org ✅

project_id         = "edutokens-2026"
region             = "us-central1"
zone               = "us-central1-a"
cluster_name       = "edutokens-cluster"
domain_name        = "edutokens.duckdns.org"

# ⚠️ COMPLETAR: tu email para notificaciones de Let's Encrypt
letsencrypt_email  = "nicolas.san9@gmail.com"

# ⚠️ COMPLETAR: CIDRs del cluster GPU del profesor (pedirlas)
# Ejemplo: ["203.0.113.0/28", "198.51.100.0/28"]
gpu_source_ranges  = []

# Free tier quotas — no tocar salvo que necesites ajustar
node_machine_type  = "e2-standard-2"
node_count         = 2
node_disk_size_gb  = 30
