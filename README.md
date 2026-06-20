# EduTokens — Blockchain Educativa Distribuida

Prototipo end-to-end de una blockchain con Proof-of-Work (MD5) minada en GPU vía CUDA, ejecutándose en Google Kubernetes Engine con CI/CD automatizado.

**Curso:** Sistemas Distribuidos y Programación Paralela (SDyPP) — UNLu  
**Deadline:** 2026-06-23

---

## Pilares

| Pilar | Tema | Estado |
|---|---|---|
| [Pilar 1](pilar1/) | CUDA GPU miner (MD5 PoW, Thrust, benchmarks CPU vs GPU) | ✅ |
| [Pilar 2](pilar2/) | Microservicios distribuidos (Python, FastAPI, RabbitMQ, Redis, Docker Compose) | ✅ |
| [Pilar 3](pilar3/) | Cloud deployment (GKE, OpenTofu, Kubernetes, CI/CD) | ✅ |

---

## Documentación

- **[project_overview.md](project_overview.md)** — Arquitectura completa, deep dive de cada pilar, decisiones de diseño
- **[pilar3/README.md](pilar3/README.md)** — Guía paso a paso de despliegue en GKE
- **[pilar2/README.md](pilar2/README.md)** — Reporte de Pilar 2 (decisiones de diseño por paso)
- **[pilar1/README.md](pilar1/README.md)** — Reporte de Pilar 1 (benchmarks CPU vs GPU)

---

## CI/CD

[![CI — Build & Push](https://github.com/nicodigo/EduTokens-blockchain/actions/workflows/ci.yml/badge.svg)](https://github.com/nicodigo/EduTokens-blockchain/actions/workflows/ci.yml)
[![Gitleaks](https://github.com/nicodigo/EduTokens-blockchain/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/nicodigo/EduTokens-blockchain/actions/workflows/gitleaks.yml)

---

## Tech Stack

| Capa | Tecnología |
|---|---|
| GPU Mining | CUDA C++ (nvcc), MD5 custom |
| CPU Mining | Python 3.12 hashlib |
| Servicios | Python 3.12, FastAPI + uvicorn |
| Mensajería | RabbitMQ 3 (pika), topic exchange |
| Almacenamiento | Redis 7 (redis-py), AOF |
| Firmas | Ed25519 (cryptography) |
| Containers | Docker + Docker Compose |
| Cloud | Google Kubernetes Engine (GKE) |
| IaC | OpenTofu |
| CI/CD | GitHub Actions + Workload Identity Federation |
| TLS | cert-manager + Let's Encrypt |
| Registry | Artifact Registry (Docker) |
