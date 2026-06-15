# =============================================================================
# Saathi — Production Dockerfile
# =============================================================================
# Multi-stage build:
#   builder  — installs Python dependencies via uv
#   runtime  — lean image with app code + venv only
#
# Image size target: ~600 MB  (Presidio spaCy models and Vertex AI SDK are heavy)
#
# Quick build & run:
#   docker build -t saathi:latest .
#   docker run -p 8000:8000 --env-file .env saathi:latest
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

# Install production deps into .venv
# --frozen       : use exact lock file if present (recommended for CI / prod)
# --no-dev       : skip test / lint tooling
# --no-install-project : install deps only; project itself is installed next
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project 2>/dev/null || true && \
    ([ -f uv.lock ] && uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project)

# Copy application source and install the project package
COPY app/          ./app/
COPY flows/        ./flows/
COPY prompts/      ./prompts/
COPY integrations/ ./integrations/
COPY dev_ui/       ./dev_ui/
COPY alembic/      ./alembic/
COPY alembic.ini*  ./

RUN --mount=type=cache,target=/root/.cache/uv \
    ([ -f uv.lock ] && uv sync --frozen --no-dev || uv sync --no-dev)

# ── Stage 2: lean runtime image ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Saathi — iGOT Karmayogi Support Chatbot"
LABEL org.opencontainers.image.description="LangGraph + YAML deterministic-first chatbot for iGOT Karmayogi Bharat"
LABEL org.opencontainers.image.source="https://github.com/aswinpradeep/igot_deterministic_chatbot"

# Minimal runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — never run as root in production
RUN groupadd -r saathi && useradd -r -g saathi -d /app -s /sbin/nologin saathi

WORKDIR /app

# Copy venv and source from builder
COPY --from=builder --chown=saathi:saathi /build/.venv       ./.venv
COPY --from=builder --chown=saathi:saathi /build/app/        ./app/
COPY --from=builder --chown=saathi:saathi /build/flows/      ./flows/
COPY --from=builder --chown=saathi:saathi /build/prompts/    ./prompts/
COPY --from=builder --chown=saathi:saathi /build/integrations/ ./integrations/
COPY --from=builder --chown=saathi:saathi /build/dev_ui/     ./dev_ui/
COPY --from=builder --chown=saathi:saathi /build/alembic/    ./alembic/
COPY --from=builder --chown=saathi:saathi /build/alembic.ini* ./

# Entrypoint script (DB migrations + server start)
COPY --chown=saathi:saathi entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# Python runtime flags
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

USER saathi

EXPOSE 8000

# Liveness check — same endpoint the load balancer pings
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info", \
     "--no-access-log"]
