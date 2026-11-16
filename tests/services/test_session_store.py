"""Redis-backed session metadata: SessionStore round-trip and the route helpers
that keep the per-pod dict as a fallback only when Redis fails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.routes import _delete_session, _load_session, _save_session
from app.services.session_store import SessionStore


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    async def setex(self, key, ttl, value):
        self.data[key] = value.encode()
        self.ttl[key] = ttl

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


class DownRedis:
    async def setex(self, *a):
        raise ConnectionError("redis down")

    async def get(self, *a):
        raise ConnectionError("redis down")

    async def delete(self, *a):
        raise ConnectionError("redis down")

    async def expire(self, *a):
        raise ConnectionError("redis down")


def _meta() -> dict:
    return {
        "user_id_hash": "h",
        "status": "in_flow",
        "flow_id": "f1",
        "ttl_minutes": 30,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "_lf_trace_id": None,
    }


def _request(store) -> SimpleNamespace:
    state = SimpleNamespace(sessions={}, session_store=store)
    return SimpleNamespace(app=SimpleNamespace(state=state))


# ── SessionStore ─────────────────────────────────────────────────────────────

async def test_meta_round_trip_restores_datetime_and_sets_ttl():
    redis = FakeRedis()
    store = SessionStore(redis, "ns")
    meta = _meta()

    assert await store.save_meta("s1", meta, 30) is True
    assert redis.ttl["ns:session:s1"] == 1800

    got = await store.get_meta("s1")
    assert got["expires_at"] == meta["expires_at"]
    assert isinstance(got["expires_at"], datetime)
    assert got["flow_id"] == "f1" and got["_lf_trace_id"] is None


async def test_meta_missing_and_delete():
    store = SessionStore(FakeRedis(), "ns")
    assert await store.get_meta("nope") is None
    await store.save_meta("s1", _meta(), 30)
    await store.delete_meta("s1")
    assert await store.get_meta("s1") is None


async def test_meta_redis_down_is_fail_safe():
    store = SessionStore(DownRedis(), "ns")
    assert await store.save_meta("s1", _meta(), 30) is False
    assert await store.get_meta("s1") is None
    await store.delete_meta("s1")  # must not raise


# ── Route helpers ────────────────────────────────────────────────────────────

async def test_pod_b_sees_session_saved_by_pod_a():
    redis = FakeRedis()
    pod_a, pod_b = _request(SessionStore(redis, "ns")), _request(SessionStore(redis, "ns"))

    await _save_session(pod_a, "s1", _meta())

    assert (await _load_session(pod_b, "s1"))["flow_id"] == "f1"
    # Redis holds it, so no local copy is kept on either pod.
    assert pod_a.app.state.sessions == {} and pod_b.app.state.sessions == {}


async def test_local_fallback_used_only_when_redis_fails():
    req = _request(SessionStore(DownRedis(), "ns"))

    await _save_session(req, "s1", _meta())

    assert "s1" in req.app.state.sessions
    assert (await _load_session(req, "s1"))["flow_id"] == "f1"


async def test_no_store_configured_uses_local_dict():
    req = _request(None)
    await _save_session(req, "s1", _meta())
    assert (await _load_session(req, "s1"))["flow_id"] == "f1"


async def test_successful_save_drops_stale_local_copy():
    redis = FakeRedis()
    req = _request(SessionStore(redis, "ns"))
    req.app.state.sessions["s1"] = {"stale": True}  # left from an earlier Redis outage

    await _save_session(req, "s1", _meta())

    assert "s1" not in req.app.state.sessions
    assert (await _load_session(req, "s1"))["flow_id"] == "f1"


async def test_delete_removes_from_redis_and_local():
    redis = FakeRedis()
    req = _request(SessionStore(redis, "ns"))
    await _save_session(req, "s1", _meta())
    req.app.state.sessions["s1"] = {"x": 1}

    await _delete_session(req, "s1")

    assert await _load_session(req, "s1") is None


# ── Rest of SessionStore (user→session mapping) and edge cases ────────────────

async def test_non_serialisable_meta_returns_false_instead_of_raising():
    store = SessionStore(FakeRedis(), "ns")
    assert await store.save_meta("s1", {"bad": object()}, 30) is False


async def test_corrupt_meta_is_treated_as_missing():
    redis = FakeRedis()
    redis.data["ns:session:s1"] = b"{not json"
    assert await SessionStore(redis, "ns").get_meta("s1") is None


class _MappingRedis(FakeRedis):
    async def expire(self, key, ttl):
        self.ttl[key] = ttl


async def test_user_session_mapping_register_get_refresh_delete():
    redis = _MappingRedis()
    store = SessionStore(redis, "ns")

    await store.register("u1", "sid-1", 30)
    assert redis.ttl["ns:user_session:u1"] == 1800
    assert await store.get_active("u1") == "sid-1"

    await store.refresh("u1", 60)
    assert redis.ttl["ns:user_session:u1"] == 3600

    await store.delete("u1")
    assert await store.get_active("u1") is None


async def test_user_session_mapping_is_fail_safe_when_redis_down():
    store = SessionStore(DownRedis(), "ns")
    await store.register("u1", "sid-1", 30)
    assert await store.get_active("u1") is None
    await store.refresh("u1", 30)
    await store.delete("u1")
