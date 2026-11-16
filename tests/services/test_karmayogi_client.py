"""Karmayogi client behaviour (204 is raised as NoContentResponse; never swallowed by the client)."""

from __future__ import annotations

import httpx
import pytest

from app.services.karmayogi import KarmayogiService


def _client_with(handler) -> KarmayogiService:
    service = KarmayogiService()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    return service


async def test_204_raises_no_content_response():
    from app.engine.nodes.api_call_node import NoContentResponse

    k = _client_with(lambda req: httpx.Response(204))
    with pytest.raises(NoContentResponse):
        await k.execute_request("GET", "/x")


@pytest.mark.parametrize("content", [b"", b"  \n", b"<html>bad gateway</html>"])
async def test_200_empty_or_malformed_body_raises_plain_decode_error(content):
    import json

    from app.engine.nodes.api_call_node import NoContentResponse

    k = _client_with(lambda req: httpx.Response(200, content=content))
    with pytest.raises(json.JSONDecodeError) as ei:
        await k.execute_request("GET", "/x")
    assert not isinstance(ei.value, NoContentResponse)


async def test_normal_envelope_still_unwrapped():
    k = _client_with(lambda req: httpx.Response(200, json={"result": {"response": {"a": 1}}}))
    out = await k.execute_request("GET", "/x")
    assert out["response"] == {"a": 1}


# ---------------------------------------------------------------------------
# Remaining execute_request / client paths (keeps karmayogi.py at 100% coverage)
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_sleep(monkeypatch):
    async def _sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)


async def test_404_raises_integration_not_found():
    from app.engine.nodes.api_call_node import IntegrationNotFound

    k = _client_with(lambda req: httpx.Response(404))
    with pytest.raises(IntegrationNotFound):
        await k.execute_request("GET", "/x")


async def test_4xx_not_retried_and_raises():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, text="bad")

    k = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await k.execute_request("GET", "/x")
    assert len(calls) == 1


async def test_5xx_retried_then_succeeds(no_sleep):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    k = _client_with(handler)
    assert await k.execute_request("GET", "/x") == {"ok": True}
    assert len(calls) == 3


async def test_5xx_exhausts_retries_and_raises(no_sleep):
    k = _client_with(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await k.execute_request("GET", "/x")


async def test_network_error_retried_then_raises(no_sleep):
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("boom")

    k = _client_with(handler)
    with pytest.raises(httpx.ConnectError):
        await k.execute_request("GET", "/x")
    assert len(calls) == 3


async def test_question_set_merged_into_content_list():
    payload = {"result": {"QuestionSet": [{"n": 1}], "content": [{"n": 0}]}}
    k = _client_with(lambda req: httpx.Response(200, json=payload))
    out = await k.execute_request("GET", "/x")
    assert out["content"] == [{"n": 0}, {"n": 1}]


async def test_question_set_creates_content_when_missing():
    payload = {"result": {"QuestionSet": [{"n": 1}]}}
    k = _client_with(lambda req: httpx.Response(200, json=payload))
    out = await k.execute_request("GET", "/x")
    assert out["content"] == [{"n": 1}]


async def test_non_envelope_json_returned_as_is():
    k = _client_with(lambda req: httpx.Response(200, json=[1, 2]))
    assert await k.execute_request("GET", "/x") == [1, 2]


async def test_get_client_creates_and_reuses_client_and_aclose():
    k = KarmayogiService()
    c1 = await k._get_client()
    assert await k._get_client() is c1
    await k.aclose()
    assert k._client is None
    await k.aclose()  # idempotent
