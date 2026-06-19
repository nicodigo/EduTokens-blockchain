# ── Intencionalmente eliminado ───────────────────────────────────
# Las reglas de firewall fueron removidas porque:
# 1. GKE auto-crea reglas para LoadBalancer y SSH/IAP
# 2. La restricción por IP ya está en el Service (loadBalancerSourceRanges)
# 3. Eran defense-in-depth redundante que no agregaba protección real
# ───────────────────────────────────────────────────────────────────
