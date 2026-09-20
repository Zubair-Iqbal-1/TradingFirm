# Three stages, mirroring services/risk-shield/Dockerfile (Part 4.1):
#   base  — system + runtime Python deps, no application code
#   dev   — base + requirements-dev.txt (pytest). Deliberately NO `COPY . .`:
#           the image is inert on its own; docker-compose.yml mounts the
#           source tree over /app for the `ai-agent-dev` service.
#   prod  — base + application code. Last stage, so an untargeted build and
#           the `ai-agent` service both get this one.
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

EXPOSE 8004

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]


FROM base AS prod

COPY . .

EXPOSE 8004

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]
