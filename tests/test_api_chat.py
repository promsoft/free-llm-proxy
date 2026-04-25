import json
from pathlib import Path

import httpx
import pytest
import respx

from free_llm_proxy.config import get_settings
from free_llm_proxy.models import TopModelsResponse

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"


def _completion(model_id: str, content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
async def loaded_app(app, fixture_payload):
    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    return app


@pytest.fixture
async def loaded_client(loaded_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=loaded_app), base_url="http://test"
    ) as c:
        yield c


def _chat_url() -> str:
    return f"{get_settings().upstream_base_url}/chat/completions"


@respx.mock
async def test_happy_path_returns_first_model_response(loaded_app, loaded_client, auth_headers):
    first_id = loaded_app.state.registry.snapshot.models[0].id
    respx.post(_chat_url()).mock(return_value=httpx.Response(200, json=_completion(first_id)))
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == first_id
    assert r.json()["choices"][0]["message"]["content"] == "ok"


@respx.mock
async def test_fallback_after_429(loaded_app, loaded_client, auth_headers):
    snap = loaded_app.state.registry.snapshot
    first_id, second_id = snap.models[0].id, snap.models[1].id
    seq = [
        httpx.Response(429, headers={"Retry-After": "300"}, json={"error": {"message": "rl"}}),
        httpx.Response(200, json=_completion(second_id)),
    ]
    respx.post(_chat_url()).mock(side_effect=seq)

    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == second_id
    # First model should be in cooldown now
    assert first_id in loaded_app.state.registry.cooldowns.until


@respx.mock
async def test_fallback_after_500(loaded_app, loaded_client, auth_headers):
    snap = loaded_app.state.registry.snapshot
    second_id = snap.models[1].id
    respx.post(_chat_url()).mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json=_completion(second_id)),
        ]
    )
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == second_id


@respx.mock
async def test_4xx_other_than_429_propagates_without_fallback(
    loaded_app, loaded_client, auth_headers
):
    snap = loaded_app.state.registry.snapshot
    first_id = snap.models[0].id
    route = respx.post(_chat_url()).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "bad request", "type": "invalid"}}
        )
    )
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    assert route.call_count == 1  # no fallback
    assert first_id not in loaded_app.state.registry.cooldowns.until


@respx.mock
async def test_all_models_unavailable_returns_503(loaded_app, loaded_client, auth_headers):
    respx.post(_chat_url()).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"message": "rl"}}
        )
    )
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "all_models_unavailable"


def _sse_stream_payload(model_id: str, content: str = "Hello") -> bytes:
    chunks = [
        {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    body = b""
    for c in chunks:
        body += f"data: {json.dumps(c)}\n\n".encode()
    body += b"data: [DONE]\n\n"
    return body


def _sse_response(model_id: str, content: str = "Hello") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse_stream_payload(model_id, content),
    )


@respx.mock
async def test_streaming_happy_path(loaded_app, loaded_client, auth_headers):
    first_id = loaded_app.state.registry.snapshot.models[0].id
    respx.post(_chat_url()).mock(return_value=_sse_response(first_id, "Hi"))
    async with loaded_client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-free-llm-proxy-model"] == first_id
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for piece in r.aiter_bytes():
            body += piece
    text = body.decode()
    assert text.count("data:") >= 4  # 3 chunks + [DONE]
    assert text.endswith("data: [DONE]\n\n")
    assert '"content": "Hi"' in text


@respx.mock
async def test_streaming_fallback_on_429(loaded_app, loaded_client, auth_headers):
    snap = loaded_app.state.registry.snapshot
    first_id, second_id = snap.models[0].id, snap.models[1].id
    respx.post(_chat_url()).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"message": "rl"}}),
            _sse_response(second_id, "Yo"),
        ]
    )
    async with loaded_client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-free-llm-proxy-model"] == second_id
        body = b""
        async for piece in r.aiter_bytes():
            body += piece
    assert b"data: [DONE]" in body
    assert first_id in loaded_app.state.registry.cooldowns.until


@respx.mock
async def test_streaming_4xx_propagates_without_fallback(loaded_app, loaded_client, auth_headers):
    route = respx.post(_chat_url()).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert r.status_code == 400
    assert route.call_count == 1


@respx.mock
async def test_streaming_all_unavailable_503(loaded_client, auth_headers):
    respx.post(_chat_url()).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"message": "rl"}}
        )
    )
    r = await loaded_client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "all_models_unavailable"


async def test_no_capable_model_returns_400(app, client, auth_headers):
    from free_llm_proxy.models import Model

    await app.state.registry.replace_snapshot([Model(rank=1, id="x/y:free", supportsTools=False)])
    r = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "no_capable_model"


async def test_no_snapshot_returns_503(client, auth_headers):
    r = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "not_ready"


async def test_unauthenticated_chat_returns_401(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


@respx.mock
async def test_api_v1_alias_works(loaded_app, loaded_client, auth_headers):
    first_id = loaded_app.state.registry.snapshot.models[0].id
    respx.post(_chat_url()).mock(return_value=httpx.Response(200, json=_completion(first_id)))
    r = await loaded_client.post(
        "/api/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == first_id


@respx.mock
async def test_capability_filter_routes_around_unsupported_first_model(app, client, auth_headers):
    from free_llm_proxy.models import Model

    await app.state.registry.replace_snapshot(
        [
            Model(rank=1, id="x/no-tools:free", supportsTools=False),
            Model(rank=2, id="x/tools:free", supportsTools=True),
        ]
    )
    respx.post(_chat_url()).mock(return_value=httpx.Response(200, json=_completion("x/tools:free")))
    r = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        },
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == "x/tools:free"
