"""Live tests — opt-in via `pytest -m live`.

Требуют:
  - OPENROUTER_API_KEY, PROXY_API_KEY в env;
  - запущенный proxy на http://localhost:8080 (docker compose up).
"""

import os

import httpx
import pytest

from free_llm_proxy.models import TopModelsResponse

pytestmark = pytest.mark.live

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8080")
SHIR_MAN_URL = "https://shir-man.com/api/free-llm/top-models"

PROMPT = "Сколько букв р в слове трансфорррмер?"  # noqa: RUF001


@pytest.fixture(scope="module")
def proxy_key() -> str:
    key = os.environ.get("PROXY_API_KEY")
    if not key:
        pytest.skip("PROXY_API_KEY not set")
    return key


def test_smoke_three_consecutive_calls(proxy_key: str):
    seen_models: list[str] = []
    with httpx.Client(timeout=60.0) as c:
        for _ in range(3):
            r = c.post(
                f"{PROXY_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {proxy_key}"},
                json={"messages": [{"role": "user", "content": PROMPT}]},
            )
            assert r.status_code == 200, r.text
            assert "x-free-llm-proxy-model" in r.headers
            seen_models.append(r.headers["x-free-llm-proxy-model"])
            content = r.json()["choices"][0]["message"]["content"]
            assert isinstance(content, str) and content.strip() != ""
    print("Models used:", seen_models)


def test_schema_real_shir_man_response_parses():
    r = httpx.get(SHIR_MAN_URL, timeout=30.0)
    assert r.status_code == 200
    parsed = TopModelsResponse.model_validate(r.json())
    assert len(parsed.models) > 0
    for m in parsed.models:
        assert m.id
        assert m.rank >= 1
