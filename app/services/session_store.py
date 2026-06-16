"""Redis-backed user→session mapping.

Stores one active session ID per user so any device/pod can resume it.
All methods are fail-safe: if Redis is unreachable they log a warning and
return a safe default (None / no-op). Nothing in the call path will break.

Key format:  {namespace}:user_session:{user_id_hash}
Value:       session_id (string UUID)
TTL:         sliding — refreshed on every user turn via refresh()
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


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

    async def delete(self, user_id_hash: str) -> None:
        """Remove the mapping when a session reaches a terminal state."""
        try:
            await self._redis.delete(self._key(user_id_hash))
            log.debug("[session_store] deleted session for user=%s", user_id_hash[:8])
        except Exception as exc:  # noqa: BLE001
            log.warning("[session_store] delete failed: %s", exc)
