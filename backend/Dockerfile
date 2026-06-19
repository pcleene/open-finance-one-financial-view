# Acme-OFV backend image — one image runs the API, mock OFP, eraser worker, and
# any -m module (different `command:` per compose service).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/backend \
    PATH=/app/backend/.venv/bin:$PATH

WORKDIR /app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv

# deps first (layer cache), then the source
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --frozen || uv sync --no-dev
COPY . .
RUN uv sync --no-dev --frozen || uv sync --no-dev

EXPOSE 8010 8100

# default = the DC API; mock/worker override `command` in compose
CMD ["gunicorn", "acme_ofv.api.app:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8010", "-w", "2", "--timeout", "120"]
