import json
from pathlib import Path

import httpx
import pytest
import respx

from free_llm_proxy.models import TopModelsResponse

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


async def test_health_always_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_503_when_no_snapshot(client):
    r = await client.get("/ready")
    assert r.status_code == 503


async def test_ready_200_after_snapshot(app, client, fixture_payload):
    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["models"] == len(parsed.models)


@respx.mock
async def test_admin_refresh_requires_auth(client, fixture_payload):
    r = await client.post("/admin/refresh")
    assert r.status_code == 401


@respx.mock
async def test_admin_refresh_pulls_snapshot(client, auth_headers, fixture_payload):
    from free_llm_proxy.config import get_settings

    respx.get(get_settings().models_list_url).mock(
        return_value=httpx.Response(200, json=fixture_payload)
    )
    r = await client.post("/admin/refresh", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == len(fixture_payload["models"])
    assert body["fetched_at"] is not None


@respx.mock
async def test_admin_refresh_resets_cooldowns(app, client, auth_headers, fixture_payload):
    from datetime import UTC, datetime, timedelta

    from free_llm_proxy.config import get_settings

    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    app.state.registry.cooldowns.mark(parsed.models[0].id, datetime.now(UTC) + timedelta(hours=1))
    respx.get(get_settings().models_list_url).mock(
        return_value=httpx.Response(200, json=fixture_payload)
    )
    r = await client.post("/admin/refresh", headers=auth_headers)
    assert r.status_code == 200
    assert app.state.registry.cooldowns.until == {}
