-- =============================================================================
-- Postgres init script — runs once on first container start
-- Creates extra databases needed by optional services (Langfuse).
-- The primary 'igot_chatbot' database is created by POSTGRES_DB env var.
-- =============================================================================

-- Langfuse observability DB (used when --profile observability is enabled)
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse') \gexec
