import pytest
from fastapi import Depends, FastAPI

from free_llm_proxy.auth import require_proxy_key


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_proxy_key)])
    async def protected():
        return {"ok": True}

    return app


async def test_missing_auth_returns_401(client):
    r = await client.get("/protected")
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "missing_authorization"


async def test_invalid_scheme_returns_401(client):
    r = await client.get("/protected", headers={"Authorization": "Token foo"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "invalid_authorization"


async def test_wrong_token_returns_403(client):
    r = await client.get("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "invalid_token"


async def test_correct_token_passes(client, auth_headers):
    r = await client.get("/protected", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
