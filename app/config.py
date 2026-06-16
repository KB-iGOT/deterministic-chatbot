"""iGOT Deterministic Chatbot runtime configuration loaded from environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Runtime
    igot_env: Literal["dev", "staging", "prod"] = "dev"
    igot_session_ttl_hours: int = 24
    log_level: str = "INFO"
    # File logging — set LOG_FILE=/path/to/igot-chatbot.log to enable; empty = console only
    log_file: str = ""
    # Max size per log file before rotation (bytes). Default 10 MB.
    log_file_max_bytes: int = 10 * 1024 * 1024
    # Number of rotated backup files to keep.
    log_file_backup_count: int = 5

    # Database
    postgres_url: str = "postgresql+asyncpg://igot_chatbot:password@localhost:5432/igot_chatbot"

    # Redis
    redis_url: str = "redis://localhost:6379/3"
    igot_redis_namespace: str = "igot_chatbot"

    # Auth (Karmayogi Keycloak)
    # KEYCLOAK_HOST — Keycloak / SSO server hostname.
    # JWKS URL and issuer are auto-derived from KEYCLOAK_HOST + KEYCLOAK_REALM.
    keycloak_host: str = "https://portal.igotkarmayogi.gov.in"
    keycloak_realm: str = "sunbird"
    # Set these only to override the auto-derived values.
    keycloak_jwks_url: str = ""
    keycloak_issuer: str = ""

    # Auth behaviour flags
    # Set AUTH_DISABLED=true to bypass all JWT verification (local dev only).
    auth_disabled: bool = False
    # Header the client sends the JWT in. Default matches iGOT platform convention.
    auth_header_name: str = "x-authenticated-user-token"
    # Role that must be present in the token's "user_roles" claim.
    # Leave empty to skip role check entirely.
    auth_required_role: str = ""
    # Fallback user ID when auth is disabled and no token is sent (dev/testing).
    # Maps to IGOT_TEST_USER_ID — reused from existing config.
    # (igot_test_user_id already declared below)

    # LLM provider selection
    llm_provider: Literal["vertex", "vllm"] = "vertex"
    llm_kill_switch: bool = False

    # Vertex AI
    genai_model_name: str = "gemini-2.5-flash"
    google_project_id: str = ""
    google_location: str = "asia-south1"
    google_application_credentials: str = ""

    # LLM limits
    llm_max_calls_per_session: int = 1
    llm_timeout_seconds: int = 8

    # vLLM (Phase 2.5+) — set LLM_PROVIDER=vllm and LOCAL_LLM_API_BASE to enable
    local_llm_api_base: str = ""

    # Zoho Desk
    zoho_base_url: str = "https://desk.zoho.in/api/v1"
    zoho_oauth_base: str = "https://accounts.zoho.in"
    zoho_refresh_token: str = ""
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_org_id: str = ""
    zoho_department_id: str = ""

    # Karmayogi platform APIs
    karmayogi_api_key: str = ""
    # Base URL for all Karmayogi platform API calls.
    # Internal deployments: http://kong:8000  |  Local dev: https://portal.uat.karmayogibharat.net
    karmayogi_portal_base_url: str = "http://kong:8000"

    # Langfuse (observability / tracing)
    # Set LANGFUSE_ENABLED=true + keys to activate; all other settings have safe defaults.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # LANGFUSE_HOST — Langfuse server URL. Defaults to cloud.langfuse.com when not set.
    langfuse_host: str = ""
    # Sample rate 0.0–1.0 (1.0 = trace every request, 0.1 = 10% sampling)
    langfuse_sample_rate: float = 1.0

    # Presidio
    presidio_enabled: bool = True
    presidio_language: str = "en"
    presidio_india_recognizers: str = "aadhaar,pan,ifsc,upi,in_phone,in_pin"

    # Bhashini (translation fallback — free, India-optimised)
    bhashini_udyat_key: str = ""
    bhashini_api_endpoint: str = ""
    bhashini_user_id: str = ""

    # Translation (multi-language support)
    # Provider chain: primary → google_translate → bhashini (automatic failover)
    # Fail-open: if all providers fail, original text is returned — never crash a session.
    translation_enabled: bool = True
    translation_primary: Literal["gemini", "google_translate", "bhashini"] = "gemini"
    translation_gemini_timeout_s: float = 3.0
    translation_google_translate_timeout_s: float = 2.0
    translation_bhashini_timeout_s: float = 4.0
    # google_translate_api_key: leave empty to use ADC (same GCP project — recommended)
    google_translate_api_key: str = ""

    # Session expiry (sliding TTL — reset on every user turn)
    igot_web_session_ttl_minutes: int = 30   # web + mobile app
    # WhatsApp: Meta's 24h window is the binding constraint; pass 1440 to initial_state()

    # YP (Young Professional) allocation data
    # Path to a CSV file with columns: centre_state, mdo, name, email, mobile, cc_email
    # Deploy the file out-of-band (never commit it — contains PII).
    # Leave empty to start with an empty lookup (service degrades gracefully).
    yp_allocation_file: str = ""

    # Dev / testing
    igot_test_user_id: str = ""   # IGOT_TEST_USER_ID — used as the default Bearer sub in dev

    # CORS
    cors_allowed_origins: str = "http://localhost:3000"

    @model_validator(mode="after")
    def _derive_keycloak_urls(self) -> "Settings":
        """Derive keycloak_jwks_url and keycloak_issuer from KEYCLOAK_HOST + KEYCLOAK_REALM."""
        realm_base = f"{self.keycloak_host.rstrip('/')}/auth/realms/{self.keycloak_realm}"
        if not self.keycloak_jwks_url:
            self.keycloak_jwks_url = f"{realm_base}/protocol/openid-connect/certs"
        if not self.keycloak_issuer:
            self.keycloak_issuer = realm_base
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent


settings = Settings()
