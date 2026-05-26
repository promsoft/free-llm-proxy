COUNT_URL = "/api/anthropic/v1/messages/count_tokens"

_BODY = {"messages": [{"role": "user", "content": "hi"}]}


async def test_x_api_key_accepted(client):
    r = await client.post(COUNT_URL, headers={"x-api-key": "test-proxy-key"}, json=_BODY)
    assert r.status_code == 200
    assert "input_tokens" in r.json()


async def test_bearer_accepted(client):
    r = await client.post(COUNT_URL, headers={"Authorization": "Bearer test-proxy-key"}, json=_BODY)
    assert r.status_code == 200


async def test_missing_key_returns_401_anthropic_shape(client):
    r = await client.post(COUNT_URL, json=_BODY)
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


async def test_wrong_key_returns_401(client):
    r = await client.post(COUNT_URL, headers={"x-api-key": "nope"}, json=_BODY)
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"
