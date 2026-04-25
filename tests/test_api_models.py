import json
from pathlib import Path

import pytest

from free_llm_proxy.models import TopModelsResponse

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


async def test_models_unauthenticated(client):
    r = await client.get("/v1/models")
    assert r.status_code == 401


async def test_models_returns_503_when_no_snapshot(client, auth_headers):
    r = await client.get("/v1/models", headers=auth_headers)
    assert r.status_code == 503


async def test_models_returns_openai_format(app, client, auth_headers, fixture_payload):
    parsed = TopModelsResponse.model_validate(fixture_payload)
    await app.state.registry.replace_snapshot(parsed.models)
    r = await client.get("/v1/models", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [d["id"] for d in body["data"]]
    expected_ids = [m.id for m in sorted(parsed.models, key=lambda m: m.rank)]
    assert ids == expected_ids
    for d in body["data"]:
        assert d["object"] == "model"
        assert d["owned_by"] == "openrouter"
        assert isinstance(d["created"], int)
