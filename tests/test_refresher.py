import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from free_llm_proxy.config import Settings
from free_llm_proxy.refresher import Refresher
from free_llm_proxy.registry import ModelRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "top-models.json"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openrouter_api_key="test",
        proxy_api_key="test",
        models_refresh_sec=3600,
    )


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text())


@respx.mock
@pytest.mark.asyncio
async def test_fetch_once_populates_snapshot(settings, fixture_payload):
    respx.get(settings.models_list_url).mock(return_value=httpx.Response(200, json=fixture_payload))
    reg = ModelRegistry()
    refresher = Refresher(reg, settings)
    count = await refresher.fetch_once()
    assert count == len(fixture_payload["models"])
    snap = reg.snapshot
    assert snap is not None
    assert len(snap.models) == count
    assert snap.models[0].rank == 1


@respx.mock
@pytest.mark.asyncio
async def test_fetch_once_keeps_old_snapshot_on_http_error(settings, fixture_payload):
    reg = ModelRegistry()
    respx.get(settings.models_list_url).mock(return_value=httpx.Response(200, json=fixture_payload))
    refresher = Refresher(reg, settings)
    await refresher.fetch_once()
    old_count = len(reg.snapshot.models)

    respx.get(settings.models_list_url).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await refresher.fetch_once()
    assert reg.snapshot is not None
    assert len(reg.snapshot.models) == old_count


@respx.mock
@pytest.mark.asyncio
async def test_fetch_once_keeps_old_snapshot_on_invalid_json(settings, fixture_payload):
    reg = ModelRegistry()
    respx.get(settings.models_list_url).mock(return_value=httpx.Response(200, json=fixture_payload))
    refresher = Refresher(reg, settings)
    await refresher.fetch_once()
    snap_before = reg.snapshot

    respx.get(settings.models_list_url).mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    with pytest.raises(ValidationError):
        await refresher.fetch_once()
    assert reg.snapshot is snap_before
