"""iGOT Deterministic Chatbot API auth — Keycloak JWT validation.

Token flow:
  1. Client sends:  x-authenticated-user-token: <JWT>   (header name configurable)
  2. Extract "kid" from JWT header
  3. Fetch JWKS from KEYCLOAK_JWKS_URL → find matching public key by kid (cached in-memory)
  4. Verify RS256 signature + expiry
  5. Check "iss" matches KEYCLOAK_ISSUER
  6. If AUTH_REQUIRED_ROLE is set — check "user_roles" array contains that role
  7. Extract user UUID from "sub" claim  (format: "f:<x>:<uuid>" → take last segment)

Environment variables (all in .env):
  KEYCLOAK_JWKS_URL      JWKS endpoint for public key fetch
  KEYCLOAK_ISSUER        Full issuer URL (must match "iss" in token)
  AUTH_DISABLED          true → bypass all verification (dev/local only)
  AUTH_HEADER_NAME       Header name for token (default: x-authenticated-user-token)
  AUTH_REQUIRED_ROLE     Role that must exist in user_roles claim (empty = skip check)
  IGOT_TEST_USER_ID      Fallback user ID when auth is disabled and no token is sent
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx as _httpx
from fastapi import HTTPException, Request, status

from app.config import settings

log = logging.getLogger(__name__)

_HMAC_KEY = b"igot-chatbot-dev-secret-replace-in-prod"  # TODO: load from secret store in prod


# ---------------------------------------------------------------------------
# JWKS key cache — in-memory, per process.
# Keys are refreshed on cache miss (unknown kid).
# Single-pod deployment: fine as-is.
# Multi-pod: each pod maintains its own cache; all pods converge within one
# request cycle on key rotation. Redis-backed cache can be added later if needed.
# ---------------------------------------------------------------------------

class _KeyManager:
    """In-memory cache of RSA public keys fetched from the JWKS endpoint."""

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}

    def get(self, kid: str) -> Any | None:
        """Return cached public key for *kid*, refreshing from JWKS on miss."""
        if kid in self._keys:
            return self._keys[kid]
        log.info("[auth] kid=%r not in cache — refreshing JWKS from %s", kid, settings.keycloak_jwks_url)
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001
            log.error("[auth] JWKS refresh failed: %s", exc)
            return None
        return self._keys.get(kid)

    def _refresh(self) -> None:
        from jwt.algorithms import RSAAlgorithm
        with _httpx.Client(timeout=10) as client:
            resp = client.get(settings.keycloak_jwks_url)
        resp.raise_for_status()
        for key in resp.json().get("keys", []):
            kid = key.get("kid")
            if kid:
                self._keys[kid] = RSAAlgorithm.from_jwk(json.dumps(key))
        log.info("[auth] JWKS refreshed — %d key(s) loaded", len(self._keys))


_key_manager = _KeyManager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_user_id(raw_user_id: str) -> str:
    """HMAC-hash the user UUID for logs/state (prevents PII leakage).

    In dev/staging the raw UUID is passed through unchanged so YAML templates
    like ``{{ ctx.user_id_hash }}`` produce the real UUID that Karmayogi APIs
    expect. In prod it is hashed.
    """
    if settings.igot_env != "prod":
        return raw_user_id
    return hmac.new(_HMAC_KEY, raw_user_id.encode(), hashlib.sha256).hexdigest()


def _extract_user_id(sub: str) -> str:
    """Extract UUID from sub claim.

    iGOT Keycloak sub format: ``f:<federation-id>:<user-uuid>``
    If no colon separators, return as-is.
    """
    return sub.split(":")[-1] if ":" in sub else sub


def _check_issuer(iss: str) -> bool:
    expected = settings.keycloak_issuer.rstrip("/")
    return bool(expected) and expected.lower() == iss.lower().rstrip("/")


def _check_role(payload: dict[str, Any]) -> bool:
    required = settings.auth_required_role
    if not required:
        return True   # no role requirement configured
    roles = payload.get("user_roles", [])
    return required in roles


# ---------------------------------------------------------------------------
# Core token validator
# ---------------------------------------------------------------------------

async def _validate_token(token: str) -> dict[str, Any]:
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Malformed token header") from exc

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token header missing 'kid'")

    if not settings.keycloak_jwks_url:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="KEYCLOAK_JWKS_URL not configured")

    public_key = _key_manager.get(kid)
    if public_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Public key not found for kid={kid!r}")

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token") from exc

    if settings.keycloak_issuer and not _check_issuer(payload.get("iss", "")):
        log.warning("[auth] iss mismatch — got %r expected %r",
                    payload.get("iss"), settings.keycloak_issuer)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token issuer invalid")

    if not _check_role(payload):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: role '{settings.auth_required_role}' required",
        )

    return payload


# ---------------------------------------------------------------------------
# FastAPI dependency — drop-in replacement for the old require_jwt stub.
# Returns claims dict with at minimum {"sub": "<user-uuid>"}.
# ---------------------------------------------------------------------------

async def require_jwt(request: Request) -> dict[str, Any]:
    """FastAPI dependency: validates the JWT and returns claims.

    Header name is configurable via AUTH_HEADER_NAME (default: x-authenticated-user-token).

    When AUTH_DISABLED=true:
      - Any token present → used as user_id directly (dev convenience)
      - No token         → falls back to IGOT_TEST_USER_ID from .env
      - Neither set      → returns "dev-stub" (no real Karmayogi data)
    """
    header_name = settings.auth_header_name  # e.g. "x-authenticated-user-token"
    token = request.headers.get(header_name, "").strip()

    # ── Dev / disabled mode ──────────────────────────────────────────────────
    if settings.auth_disabled:
        if token and token != "dev-stub":
            user_id = token   # treat raw token as user UUID (dev shortcut)
        elif settings.igot_test_user_id:
            user_id = settings.igot_test_user_id
        else:
            user_id = "dev-stub"
        log.debug("[auth] AUTH_DISABLED — using user_id=%r", user_id)
        return {"sub": user_id, "preferred_username": user_id[:8]}

    # ── Production: full JWT validation ─────────────────────────────────────
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing authentication token (expected header: {header_name})",
        )

    payload = await _validate_token(token)
    raw_sub = payload.get("sub", "")
    user_id = _extract_user_id(raw_sub)

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token 'sub' claim is empty")

    log.debug("[auth] validated user_id=%r", user_id)
    return payload | {"sub": user_id}   # ensure sub is always the clean UUID
