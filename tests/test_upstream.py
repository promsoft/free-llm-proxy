from datetime import UTC, datetime

import httpx
import pytest
import respx

from free_llm_proxy.config import Settings
from free_llm_proxy.upstream import Outcome, Upstream, UpstreamError, parse_retry_after

NOW = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


def test_parse_retry_after_seconds():
    out = parse_retry_after({"retry-after": "7"}, NOW)
    assert out == datetime(2026, 4, 25, 12, 0, 7, tzinfo=UTC)


def test_parse_retry_after_negative_treated_as_zero():
    out = parse_retry_after({"retry-after": "-5"}, NOW)
    assert out == NOW


def test_parse_retry_after_http_date():
    out = parse_retry_after({"retry-after": "Sat, 25 Apr 2026 12:00:30 GMT"}, NOW)
    assert out == datetime(2026, 4, 25, 12, 0, 30, tzinfo=UTC)


def test_parse_retry_after_x_ratelimit_reset_seconds():
    ts = int(NOW.timestamp()) + 60
    out = parse_retry_after({"x-ratelimit-reset": str(ts)}, NOW)
    assert out == datetime.fromtimestamp(ts, tz=UTC)


def test_parse_retry_after_x_ratelimit_reset_ms():
    ts_ms = int(NOW.timestamp() * 1000) + 60_000
    out = parse_retry_after({"x-ratelimit-reset": str(ts_ms)}, NOW)
    assert abs((out - NOW).total_seconds() - 60) < 1


def test_parse_retry_after_absent():
    assert parse_retry_after({}, NOW) is None


def test_parse_retry_after_garbage_falls_back_to_none():
    assert parse_retry_after({"retry-after": "soon-ish"}, NOW) is None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openrouter_api_key="test-key",
        proxy_api_key="test",
        upstream_base_url="https://api.openrouter.test/v1",
        upstream_timeout_sec=5.0,
    )


def _completion_payload() -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "stub",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@respx.mock
@pytest.mark.asyncio
async def test_chat_success(settings):
    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_payload())
    )
    up = Upstream(settings)
    out = await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert out["choices"][0]["message"]["content"] == "hi"


@respx.mock
@pytest.mark.asyncio
async def test_chat_rate_limit_classified_with_retry_after(settings):
    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "7"}, json={"error": {"message": "slow"}}
        )
    )
    up = Upstream(settings)
    with pytest.raises(UpstreamError) as exc:
        await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert exc.value.outcome is Outcome.RATE_LIMITED
    assert exc.value.status_code == 429
    assert exc.value.retry_after is not None


@respx.mock
@pytest.mark.asyncio
async def test_chat_503_classified_as_upstream_error(settings):
    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    up = Upstream(settings)
    with pytest.raises(UpstreamError) as exc:
        await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert exc.value.outcome is Outcome.UPSTREAM_ERROR
    assert exc.value.status_code == 503


@respx.mock
@pytest.mark.asyncio
async def test_chat_500_classified_as_upstream_error(settings):
    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )
    up = Upstream(settings)
    with pytest.raises(UpstreamError) as exc:
        await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert exc.value.outcome is Outcome.UPSTREAM_ERROR


@respx.mock
@pytest.mark.asyncio
async def test_chat_400_propagated_as_client_error(settings):
    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    up = Upstream(settings)
    with pytest.raises(UpstreamError) as exc:
        await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert exc.value.outcome is Outcome.CLIENT_ERROR
    assert exc.value.status_code == 400


@respx.mock
@pytest.mark.asyncio
async def test_chat_passes_authorization_and_default_headers(settings):
    seen: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["referer"] = request.headers.get("http-referer")
        seen["title"] = request.headers.get("x-title")
        return httpx.Response(200, json=_completion_payload())

    respx.post(f"{settings.upstream_base_url}/chat/completions").mock(side_effect=capture)
    up = Upstream(settings)
    await up.chat("provider/model", {"messages": [{"role": "user", "content": "hi"}]})
    await up.aclose()
    assert seen["auth"] == f"Bearer {settings.openrouter_api_key}"
    assert seen["referer"] == settings.openrouter_referer
    assert seen["title"] == settings.openrouter_title
