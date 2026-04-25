import json
from pathlib import Path

import httpx
import pytest
import respx

from free_llm_proxy.config import get_settings
from free_llm_proxy.models import TopModelsResponse

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _completion(model_id: str) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": model_id,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


async def test_metrics_endpoint_no_auth_returns_prometheus(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "freellm_active_models" in r.text
    assert r.headers["content-type"].startswith("text/plain")


async def test_metrics_reports_active_and_age(app, client, fixture_payload):
    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    r = await client.get("/metrics")
    text = r.text
    assert f"freellm_active_models {float(len(parsed.models))}" in text
    assert "freellm_snapshot_age_seconds" in text


@respx.mock
async def test_metrics_increment_after_chat_request(app, client, auth_headers, fixture_payload):
    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    first_id = parsed.models[0].id
    respx.post(f"{get_settings().upstream_base_url}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion(first_id))
    )
    await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    r = await client.get("/metrics")
    text = r.text
    assert "freellm_requests_total" in text
    assert 'status="200"' in text
    assert "freellm_upstream_attempts_total" in text
