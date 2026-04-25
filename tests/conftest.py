import os
from collections.abc import AsyncIterator

import httpx
import pytest

os.environ.setdefault("OPENROUTER_API_KEY", "test-or-key")
os.environ.setdefault("PROXY_API_KEY", "test-proxy-key")
os.environ.setdefault("MODELS_REFRESH_SEC", "3600")

from free_llm_proxy.config import reset_settings_cache
from free_llm_proxy.main import create_app


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def app():
    return create_app(auto_start_refresher=False)


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-proxy-key"}
