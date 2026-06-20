# Worker GPU Dockerfile — NVIDIA RTX 4060 (sm_89), CUDA 12.2
# Build context: repo root (EduTokens-blockchain/)
FROM nvidia/cuda:12.2.2-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Worker Python code (same modules as worker-cpu)
COPY pilar2/shared/ shared/
COPY pilar2/broker/ broker/
COPY pilar2/miner/ miner/
COPY pilar2/worker/ worker/

# CUDA binary optimized for RTX 4060 (sm_89)
COPY pilar1/md5_range_4060/md5_range /app/md5_range

RUN pip3 install --no-cache-dir pika fastapi uvicorn cryptography

ENV PYTHONPATH=/app
ENV MINER_BINARY=/app/md5_range

CMD ["python3", "-m", "worker.worker"]
