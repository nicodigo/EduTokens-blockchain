# cert-manager y nginx-ingress vía Helm
# cert-manager: emite certificados TLS automáticamente vía Let's Encrypt
# nginx-ingress: Ingress Controller para tráfico HTTP/HTTPS

# --- cert-manager ---
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  wait             = true

  set {
    name  = "installCRDs"
    value = "true"
  }

  depends_on = [google_container_node_pool.infra_apps]
}

# --- ClusterIssuer: Let's Encrypt Production ---
# Usa HTTP-01 challenge vía el nginx-ingress
resource "kubernetes_manifest" "cluster_issuer" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt-prod"
    }
    spec = {
      acme = {
        server = "https://acme-v02.api.letsencrypt.org/directory"
        email  = var.letsencrypt_email
        privateKeySecretRef = {
          name = "letsencrypt-prod-account-key"
        }
        solvers = [
          {
            http01 = {
              ingress = {
                class = "nginx"
              }
            }
          }
        ]
      }
    }
  }

  depends_on = [helm_release.cert_manager]
}

# --- nginx-ingress ---
resource "helm_release" "nginx_ingress" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  wait             = true

  # Free tier: mínimo de recursos
  set {
    name  = "controller.resources.requests.cpu"
    value = "100m"
  }
  set {
    name  = "controller.resources.requests.memory"
    value = "128Mi"
  }
  set {
    name  = "controller.resources.limits.cpu"
    value = "500m"
  }
  set {
    name  = "controller.resources.limits.memory"
    value = "256Mi"
  }

  set {
    name  = "controller.service.type"
    value = "LoadBalancer"
  }

  # Permitir WebSocket (RabbitMQ Management)
  set {
    name  = "controller.config.proxy-read-timeout"
    value = "3600"
  }
  set {
    name  = "controller.config.proxy-send-timeout"
    value = "3600"
  }

  depends_on = [google_container_node_pool.infra_apps]
}
