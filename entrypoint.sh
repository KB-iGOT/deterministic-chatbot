#!/bin/sh
# =============================================================================
# iGOT Deterministic Chatbot entrypoint
# Runs DB migrations (if enabled) then hands off to the CMD.
# =============================================================================
set -e


echo "🚀 Starting iGOT Deterministic Chatbot API…"
exec "$@"
