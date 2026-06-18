FROM python:3.12-alpine
WORKDIR /app

RUN apk add --no-cache curl

COPY shared/ shared/
COPY broker/ broker/
COPY miner/ miner/
COPY worker/ worker/

RUN pip install --no-cache-dir pika fastapi uvicorn cryptography

ENV PYTHONPATH=/app

CMD ["python", "-m", "worker.worker"]
