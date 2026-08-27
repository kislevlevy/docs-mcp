# syntax=docker/dockerfile:1

# ---------- build: deps + embedding models baked in ----------
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so edits to src/ don't re-resolve the environment.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Download both ONNX models into the image. The runtime container then needs no
# network at all, and the first query is not a silent multi-minute download.
ENV FASTEMBED_CACHE_PATH=/models
RUN /app/.venv/bin/docs-mcp warmup

# ---------- runtime ----------
FROM python:3.13-slim

# onnxruntime needs libgomp; nothing else is required.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app -d /app app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /models /models
COPY src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/models \
    HF_HUB_OFFLINE=1 \
    DOCS_DIR=/docs \
    DB_PATH=/data/index.db \
    HOST=0.0.0.0 \
    PORT=8765

# /models must be writable by the runtime user: huggingface_hub writes cache
# metadata next to the weights on every load, and a read-only tree logs a
# permission error each time the model is opened.
RUN mkdir -p /data && chown -R app:app /data /app /models

USER app
WORKDIR /app
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8765/health',timeout=8).status==200 else 1)"]

ENTRYPOINT ["docs-mcp"]
CMD ["serve"]
