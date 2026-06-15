#!/bin/sh
# =============================================================================
# iGOT Deterministic Chatbot entrypoint
# Runs DB migrations (if enabled) then hands off to the CMD.
# =============================================================================
set -e

# Run Alembic migrations before starting the server.
# Controlled by RUN_MIGRATIONS env var (default: false).
# Set RUN_MIGRATIONS=true in docker-compose for dev / staging.
# In production, prefer running migrations as a separate init-container or job.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "⏳ Running Alembic migrations…"
    alembic upgrade head
    echo "✅ Migrations complete."
fi

echo "🚀 Starting iGOT Deterministic Chatbot API…"
exec "$@"
