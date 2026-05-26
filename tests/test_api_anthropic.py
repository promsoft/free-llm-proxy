import json
from pathlib import Path

import httpx
import pytest
import respx

from free_llm_proxy.config import get_settings
from free_llm_proxy.models import Model, TopModelsResponse

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"

ANTHROPIC_URL = "/api/anthropic/v1/messages"
COUNT_URL = "/api/anthropic/v1/messages/count_tokens"


@pytest.fixture
def anthropic_headers() -> dict[str, str]:
    return {"x-api-key": "test-proxy-key", "anthropic-version": "2023-06-01"}


@pytest.fixture
async def loaded_app(app):
    parsed = TopModelsResponse.model_validate(json.loads(FIXTURE.read_text()))
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


def _completion(model_id: str, content: str = "ok", tool_calls=None, finish="stop") -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish = "tool_calls"
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model_id,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def _oai_sse_response(model_id: str, content: str = "Hello") -> httpx.Response:
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
            ],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model_id,
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    ]
    body = b""
    for chunk in chunks:
        body += f"data: {json.dumps(chunk)}\n\n".encode()
    body += b"data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def _sse_event_names(raw: bytes) -> list[str]:
    return [
        line[len("event:") :].strip()
        for line in raw.decode().splitlines()
        if line.startswith("event:")
    ]


# --- non-stream ------------------------------------------------------------ #


@respx.mock
async def test_messages_happy_path(loaded_app, loaded_client, anthropic_headers):
    first_id = loaded_app.state.registry.snapshot.models[0].id
    respx.post(_chat_url()).mock(return_value=httpx.Response(200, json=_completion(first_id)))
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={
            "model": "claude-x",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == first_id
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "ok"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert body["id"].startswith("msg_")


@respx.mock
async def test_messages_tool_use_round_trip(app, client, anthropic_headers):
    await app.state.registry.replace_snapshot(
        [Model(rank=1, id="x/tools:free", supportsTools=True)]
    )
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
        }
    ]
    respx.post(_chat_url()).mock(
        return_value=httpx.Response(200, json=_completion("x/tools:free", tool_calls=tool_calls))
    )
    r = await client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {"city": "SF"},
    }


@respx.mock
async def test_messages_fallback_after_429(loaded_app, loaded_client, anthropic_headers):
    snap = loaded_app.state.registry.snapshot
    first_id, second_id = snap.models[0].id, snap.models[1].id
    respx.post(_chat_url()).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "300"}, json={"error": {"message": "rl"}}),
            httpx.Response(200, json=_completion(second_id)),
        ]
    )
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["x-free-llm-proxy-model"] == second_id
    assert first_id in loaded_app.state.registry.cooldowns.until


@respx.mock
async def test_messages_all_unavailable_returns_503_overloaded(
    loaded_app, loaded_client, anthropic_headers
):
    respx.post(_chat_url()).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"message": "rl"}}
        )
    )
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "overloaded_error"


@respx.mock
async def test_messages_4xx_passthrough_no_fallback(loaded_app, loaded_client, anthropic_headers):
    route = respx.post(_chat_url()).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    assert route.call_count == 1
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"] == "bad request"


@respx.mock
async def test_messages_upstream_401_returns_502_api_error(
    loaded_app, loaded_client, anthropic_headers
):
    route = respx.post(_chat_url()).mock(
        return_value=httpx.Response(401, json={"error": {"message": "no auth"}})
    )
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 502
    assert route.call_count == 1
    err = r.json()["error"]
    assert err["type"] == "api_error"
    assert "OPENROUTER_API_KEY" in err["message"]
    assert "len=" in err["message"]


async def test_messages_no_capable_model_returns_400(app, client, anthropic_headers):
    await app.state.registry.replace_snapshot([Model(rank=1, id="x/y:free", supportsTools=False)])
    r = await client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "input_schema": {}}],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_messages_missing_max_tokens_returns_400(loaded_client, anthropic_headers):
    r = await loaded_client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_messages_no_snapshot_returns_503(client, anthropic_headers):
    r = await client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "overloaded_error"


# --- streaming ------------------------------------------------------------- #


@respx.mock
async def test_messages_streaming_happy_path(loaded_app, loaded_client, anthropic_headers):
    first_id = loaded_app.state.registry.snapshot.models[0].id
    respx.post(_chat_url()).mock(return_value=_oai_sse_response(first_id, "Hello"))
    async with loaded_client.stream(
        "POST",
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-free-llm-proxy-model"] == first_id
        assert r.headers["content-type"].startswith("text/event-stream")
        raw = b""
        async for piece in r.aiter_bytes():
            raw += piece

    names = _sse_event_names(raw)
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert "content_block_start" in names
    assert "content_block_delta" in names
    assert "message_delta" in names
    assert "Hello" in raw.decode()


@respx.mock
async def test_messages_streaming_fallback_on_429(loaded_app, loaded_client, anthropic_headers):
    snap = loaded_app.state.registry.snapshot
    first_id, second_id = snap.models[0].id, snap.models[1].id
    respx.post(_chat_url()).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"message": "rl"}}),
            _oai_sse_response(second_id, "Yo"),
        ]
    )
    async with loaded_client.stream(
        "POST",
        ANTHROPIC_URL,
        headers=anthropic_headers,
        json={"max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-free-llm-proxy-model"] == second_id
        raw = b""
        async for piece in r.aiter_bytes():
            raw += piece

    names = _sse_event_names(raw)
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert first_id in loaded_app.state.registry.cooldowns.until


# --- count_tokens ---------------------------------------------------------- #


async def test_count_tokens(client, anthropic_headers):
    r = await client.post(
        COUNT_URL,
        headers=anthropic_headers,
        json={"messages": [{"role": "user", "content": "hello world"}]},
    )
    assert r.status_code == 200
    assert r.json()["input_tokens"] > 0
