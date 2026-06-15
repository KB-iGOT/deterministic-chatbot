#!/bin/sh
# =============================================================================
# iGOT Deterministic Chatbot entrypoint
# Runs DB migrations (if enabled) then hands off to the CMD.
# =============================================================================
set -e

export PATH="/app/.venv/bin:$PATH"


echo "🚀 Starting iGOT Deterministic Chatbot API…"
exec "$@"
