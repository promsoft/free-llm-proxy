"""OpenRouter passthrough (/api/openrouter) — spec/openrouter.md §10."""

import json

import httpx
import pytest
import respx

from free_llm_proxy.config import reset_settings_cache
from free_llm_proxy.main import create_app

BASE = "https://openrouter.ai"


@respx.mock
async def test_passthrough_chat_completions(client, auth_headers):
    route = respx.post(f"{BASE}/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "chatcmpl-1", "model": "openai/gpt-4o"},
            headers={"X-RateLimit-Remaining": "9"},
        )
    )
    payload = {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post(
        "/api/openrouter/api/v1/chat/completions",
        headers={**auth_headers, "Content-Type": "application/json", "X-Custom": "yes"},
        content=json.dumps(payload),
    )
    assert r.status_code == 200
    assert r.json()["id"] == "chatcmpl-1"
    assert r.headers["x-ratelimit-remaining"] == "9"

    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer test-or-key"
    assert sent.headers["http-referer"] == "https://github.com/promsoft/free-llm-proxy"
    assert sent.headers["x-title"] == "free-llm-proxy"
    assert sent.headers["x-custom"] == "yes"
    assert json.loads(sent.content) == payload


@respx.mock
async def test_v1_alias_maps_to_api_v1(client, auth_headers):
    route = respx.post(f"{BASE}/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "chatcmpl-2"})
    )
    r = await client.post(
        "/api/openrouter/v1/chat/completions",
        headers=auth_headers,
        json={"model": "openai/gpt-4o", "messages": []},
    )
    assert r.status_code == 200
    assert route.called


@respx.mock
async def test_get_with_query_params(client, auth_headers):
    route = respx.get(f"{BASE}/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    r = await client.get(
        "/api/openrouter/api/v1/models",
        params={"category": "programming"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert route.calls.last.request.url.params["category"] == "programming"


@respx.mock
async def test_streaming_bytes_relayed_verbatim(client, auth_headers):
    sse = b'data: {"id":"chatcmpl-3"}\n\ndata: [DONE]\n\n'
    respx.post(f"{BASE}/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})
    )
    r = await client.post(
        "/api/openrouter/api/v1/chat/completions",
        headers=auth_headers,
        json={"model": "openai/gpt-4o", "messages": [], "stream": True},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream"
    assert r.content == sse


@respx.mock
async def test_429_passthrough_without_cooldown(app, client, auth_headers):
    respx.post(f"{BASE}/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "rate limited"}},
            headers={"Retry-After": "300"},
        )
    )
    r = await client.post(
        "/api/openrouter/api/v1/chat/completions",
        headers=auth_headers,
        json={"model": "some/model:free", "messages": []},
    )
    assert r.status_code == 429
    assert r.json()["error"]["message"] == "rate limited"
    assert r.headers["retry-after"] == "300"
    assert not app.state.registry.cooldowns.until


@respx.mock
async def test_upstream_401_becomes_502_auth_error(client, auth_headers):
    respx.get(f"{BASE}/api/v1/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    r = await client.get("/api/openrouter/api/v1/models", headers=auth_headers)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "upstream_auth_error"
    assert "OPENROUTER_API_KEY" in err["message"]


@respx.mock
async def test_network_error_becomes_502_unreachable(client, auth_headers):
    respx.get(f"{BASE}/api/v1/models").mock(side_effect=httpx.ConnectError("boom"))
    r = await client.get("/api/openrouter/api/v1/models", headers=auth_headers)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"


async def test_auth_required(client):
    r = await client.get("/api/openrouter/api/v1/models")
    assert r.status_code == 401
    r = await client.get("/api/openrouter/api/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403


@respx.mock
@pytest.mark.parametrize(
    "path",
    [
        "/api/openrouter/api/v1/credits",
        "/api/openrouter/api/v1/keys",
        "/api/openrouter/api/v1/key",
        "/api/openrouter/api/v1/keys/abc",
        "/api/openrouter/v1/keys",
        "/api/openrouter/api/v1/auth/keys",
    ],
)
async def test_blocked_paths_return_403(client, auth_headers, path):
    upstream = respx.route(host="openrouter.ai").mock(return_value=httpx.Response(200, json={}))
    r = await client.get(path, headers=auth_headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden_path"
    assert not upstream.called


@respx.mock
async def test_blocklist_matches_segments_not_substrings(client, auth_headers):
    route = respx.get(f"{BASE}/api/v1/keysmith").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    r = await client.get("/api/openrouter/api/v1/keysmith", headers=auth_headers)
    assert r.status_code == 200
    assert route.called


async def test_disabled_flag_returns_404(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROXY_ENABLED", "false")
    reset_settings_cache()
    app = create_app(auto_start_refresher=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get(
            "/api/openrouter/api/v1/models",
            headers={"Authorization": "Bearer test-proxy-key"},
        )
    assert r.status_code == 404
