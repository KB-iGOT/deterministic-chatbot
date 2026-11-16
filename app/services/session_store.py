"""Redis-backed session storage shared by all pods.

1. user→session mapping: one active session ID per user so any device/pod can resume it.
     Key:    {namespace}:user_session:{user_id_hash}
     Value:  session_id (string UUID)
     TTL:    sliding — refreshed on every user turn via refresh()

2. Per-session metadata (status, flow_id, selected_category, expires_at, ...) so a
   turn can be served by any pod, not only the one that created the session.
     Key:    {namespace}:session:{session_id}
     Value:  JSON object
     TTL:    the session TTL, re-set on every save_meta()

All methods are fail-safe: if Redis is unreachable they log a warning and
return a safe default (None / False / no-op). Nothing in the call path will break.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger(__name__)

# Metadata fields stored as ISO strings in JSON and restored to datetimes on load.
_DATETIME_FIELDS = ("expires_at",)


def _json_default(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"{type(obj).__name__} is not JSON serialisable")


class SessionStore:
    def __init__(self, client: object, namespace: str) -> None:
        self._redis = client
        self._ns = namespace

    def _key(self, user_id_hash: str) -> str:
        return f"{self._ns}:user_session:{user_id_hash}"

    async def register(self, user_id_hash: str, session_id: str, ttl_minutes: int) -> None:
        """Store user→session_id with a sliding TTL. No-op if Redis is down."""
        try:
            await self._redis.setex(self._key(user_id_hash), ttl_minutes * 60, session_id)
            log.debug("[session_store] registered session=%s user=%s ttl=%dm",
                      session_id, user_id_hash[:8], ttl_minutes)
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] register failed: %s", exc)

    async def get_active(self, user_id_hash: str) -> Optional[str]:
        """Return the active session_id for user, or None if absent / Redis down."""
        try:
            val = await self._redis.get(self._key(user_id_hash))
            if val is None:
                return None
            return val.decode() if isinstance(val, bytes) else val
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] get_active failed: %s", exc)
            return None

    async def refresh(self, user_id_hash: str, ttl_minutes: int) -> None:
        """Slide the TTL forward — call on every successful user turn."""
        try:
            await self._redis.expire(self._key(user_id_hash), ttl_minutes * 60)
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] refresh failed: %s", exc)

    # ── Per-session metadata ────────────────────────────────────────────────

    def _meta_key(self, session_id: str) -> str:
        return f"{self._ns}:session:{session_id}"

    async def save_meta(self, session_id: str, meta: dict[str, Any], ttl_minutes: int) -> bool:
        """Store session metadata with the session TTL. Returns False if it could not be saved."""
        try:
            payload = json.dumps(meta, default=_json_default)
            await self._redis.setex(self._meta_key(session_id), ttl_minutes * 60, payload)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] save_meta failed: %s", exc)
            return False

    async def get_meta(self, session_id: str) -> Optional[dict[str, Any]]:
        """Return the session metadata, or None if absent / expired / Redis down."""
        try:
            raw = await self._redis.get(self._meta_key(session_id))
            if raw is None:
                return None
            meta = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            for field in _DATETIME_FIELDS:
                if isinstance(meta.get(field), str):
                    meta[field] = datetime.fromisoformat(meta[field])
            return meta
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] get_meta failed: %s", exc)
            return None

    async def delete_meta(self, session_id: str) -> None:
        """Remove the session metadata (expired / terminal)."""
        try:
            await self._redis.delete(self._meta_key(session_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] delete_meta failed: %s", exc)

    async def delete(self, user_id_hash: str) -> None:
        """Remove the mapping when a session reaches a terminal state."""
        try:
            await self._redis.delete(self._key(user_id_hash))
            log.debug("[session_store] deleted session for user=%s", user_id_hash[:8])
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] delete failed: %s", exc)
