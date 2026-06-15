# =============================================================================
# iGOT Deterministic Chatbot — Production Dockerfile
# =============================================================================
# Multi-stage build:
#   builder  — installs Python dependencies via uv
#   runtime  — lean image with app code + venv only
#
# Image size target: ~600 MB  (Presidio spaCy models and Vertex AI SDK are heavy)
#
# Quick build & run:
#   docker build -t igot-chatbot:latest .
#   docker run -p 8000:8000 --env-file .env igot-chatbot:latest
#
# Full stack (recommended):
#   docker compose up
# =============================================================================

# ── Stage 1: dependency installer ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv — fast Rust-based Python package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# System build deps (needed for Presidio spaCy, asyncpg, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy dependency manifests — these layers are cached unless deps change
COPY pyproject.toml ./
# uv.lock is optional on first run; generate with `uv lock` and commit it
COPY uv.lock* ./

COPY README.md .

# Install production deps into .venv
# --frozen       : use exact lock file if present (recommended for CI / prod)
# --no-dev       : skip test / lint tooling
# --no-install-project : install deps only; project itself is installed next
RUN ([ -f uv.lock ] && uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project)

# Copy application source and install the project package
COPY README.md     ./
COPY app/          ./app/
COPY flows/        ./flows/
COPY prompts/      ./prompts/
COPY integrations/ ./integrations/
COPY dev_ui/       ./dev_ui/
RUN ([ -f uv.lock ] && uv sync --frozen --no-dev || uv sync --no-dev)

# ── Stage 2: lean runtime image ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="iGOT Deterministic Chatbot — iGOT Karmayogi Support Chatbot"
LABEL org.opencontainers.image.description="LangGraph + YAML deterministic-first chatbot for iGOT Karmayogi Bharat"
LABEL org.opencontainers.image.source="https://github.com/aswinpradeep/deterministic-chatbot"

# Minimal runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — never run as root in production
RUN groupadd -r igot && useradd -r -g igot -d /app -s /sbin/nologin igot

WORKDIR /app

# Pre-create writable dirs with correct ownership
# logs/ — only needed if LOG_FILE is set; in Kubernetes leave LOG_FILE unset
#          and let stdout/stderr be captured by the cluster logging stack instead.
RUN mkdir -p /app/logs && chown igot:igot /app/logs

# Copy venv and source from builder
COPY --from=builder --chown=igot:igot /build/.venv       ./.venv
COPY --from=builder --chown=igot:igot /build/app/        ./app/
COPY --from=builder --chown=igot:igot /build/flows/      ./flows/
COPY --from=builder --chown=igot:igot /build/prompts/    ./prompts/
COPY --from=builder --chown=igot:igot /build/integrations/ ./integrations/
COPY --from=builder --chown=igot:igot /build/dev_ui/     ./dev_ui/

# Entrypoint script (DB migrations + server start)
COPY --chown=igot:igot entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# Python runtime flags
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

USER igot

EXPOSE 8000

# Liveness check — same endpoint the load balancer pings
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info", \
     "--no-access-log"]
