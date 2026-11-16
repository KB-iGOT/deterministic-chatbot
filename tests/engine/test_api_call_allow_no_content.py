"""api_call `allow_no_content`: only an HTTP 204 (NoContentResponse) becomes an empty result."""

from __future__ import annotations

import json
import uuid

from app.engine.nodes.api_call_node import ApiCallNode, NoContentResponse
from app.engine.state import ConversationState


class _FakeIntegration:
    def __init__(self, exc: Exception | None = None, result=None) -> None:
        self.exc = exc
        self.result = result if result is not None else {"response": {"a": "x"}}

    async def execute_request(self, **kwargs):
        if self.exc:
            raise self.exc
        return self.result


def _cfg(**extra):
    return {
        "id": "n1",
        "integration": "karmayogi",
        "request": {"method": "GET", "url": "/x"},
        "response_mapping": [{"from": "$.response.a", "to": "collected.a"}],
        "on_success": "done",
        **extra,
    }


def _state() -> ConversationState:
    return ConversationState(session_id=uuid.uuid4(), user_id_hash="u")


async def _run(cfg, integration):
    return await ApiCallNode({"karmayogi": integration}).build(cfg)(_state())


async def test_204_with_flag_is_empty_result():
    out = await _run(_cfg(allow_no_content=True), _FakeIntegration(exc=NoContentResponse()))
    assert out["collected"]["a"] is None
    assert out["collected"]["_last_api_error"] == ""


async def test_204_without_flag_is_error():
    out = await _run(_cfg(), _FakeIntegration(exc=NoContentResponse()))
    assert out["collected"]["_last_api_error"] == "any:invalid_json"


async def test_other_empty_or_malformed_body_still_error_with_flag():
    exc = json.JSONDecodeError("Expecting value", "", 0)
    out = await _run(_cfg(allow_no_content=True), _FakeIntegration(exc=exc))
    assert out["collected"]["_last_api_error"] == "any:invalid_json"


async def test_other_errors_still_error_with_flag():
    out = await _run(_cfg(allow_no_content=True), _FakeIntegration(exc=RuntimeError("boom")))
    assert out["collected"]["_last_api_error"] == "any:boom"


async def test_normal_response_unaffected_by_flag():
    out = await _run(_cfg(allow_no_content=True), _FakeIntegration())
    assert out["collected"]["a"] == "x"
