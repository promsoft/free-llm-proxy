"""Transparent OpenRouter passthrough (/api/openrouter). See spec/openrouter.md.

No model selection, no fallback, no cooldown interaction: the client names the
model, the proxy swaps in OPENROUTER_API_KEY and relays bytes verbatim.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import require_proxy_key
from ..config import Settings, get_settings
from ..deps import get_openrouter_client
from ..logging import get_logger
from ..metrics import openrouter_proxy_requests_total
from ..upstream import upstream_auth_error_body

router = APIRouter(tags=["openrouter"], dependencies=[Depends(require_proxy_key)])
log = get_logger(__name__)

_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# Key/account management stays private to the proxy operator (spec §2.1).
_BLOCKED_SEGMENTS = {"key", "keys", "credits", "auth"}

# Request bodies above this size are relayed without buffering (and without
# best-effort `model` extraction for the log).
_BUFFER_LIMIT = 64 * 1024

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _is_blocked(tail: str) -> bool:
    segments = tuple(s.lower() for s in tail.split("/") if s)
    return segments[:2] == ("api", "v1") and len(segments) > 2 and segments[2] in _BLOCKED_SEGMENTS


def _build_upstream_headers(incoming: Mapping[str, str], settings: Settings) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": settings.openrouter_referer,
        "X-Title": settings.openrouter_title,
        "Accept-Encoding": incoming.get("accept-encoding", "identity"),
    }
    for name in ("content-type", "accept"):
        if name in incoming:
            headers[name] = incoming[name]
    for name, value in incoming.items():
        lower = name.lower()
        if lower.startswith("x-") and lower not in ("x-api-key", "x-title"):
            headers[name] = value
    return headers


def _filter_response_headers(upstream: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in upstream.items() if k.lower() not in _HOP_BY_HOP}


def _requested_model(body: bytes, content_type: str | None) -> str | None:
    """Best-effort `model` extraction for the log only; never fails the request."""
    if not body or "json" not in (content_type or ""):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) else None


def _error_json(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _finish(
    request_id: str,
    method: str,
    tail: str,
    status: int,
    started: float,
    requested_model: str | None,
) -> None:
    openrouter_proxy_requests_total.labels(method, str(status)).inc()
    log.info(
        "request_done",
        extra={
            "api": "openrouter",
            "request_id": request_id,
            "method": method,
            "path": tail,
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "requested_model": requested_model,
        },
    )


@router.api_route("/v1/{path:path}", methods=_METHODS)
async def passthrough_v1_alias(
    path: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_openrouter_client),
):
    """Alias for OpenAI-SDK clients using base_url=.../api/openrouter/v1 (spec §2)."""
    return await passthrough(f"api/v1/{path}", request, settings, client)


@router.api_route("/{path:path}", methods=_METHODS)
async def passthrough(
    path: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_openrouter_client),
):
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    method = request.method
    tail = path.lstrip("/")

    if _is_blocked(tail):
        _finish(request_id, method, tail, 403, started, None)
        return _error_json(
            "forbidden_path", "This OpenRouter path is not exposed via the proxy.", 403
        )

    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    headers = _build_upstream_headers(request.headers, settings)
    requested_model: str | None = None
    if content_length > _BUFFER_LIMIT:
        # Relay large uploads without buffering; keep the exact framing.
        content: bytes | AsyncIterator[bytes] = request.stream()
        headers["Content-Length"] = str(content_length)
    else:
        content = await request.body()
        requested_model = _requested_model(content, request.headers.get("content-type"))
    upstream_request = client.build_request(
        method,
        f"/{tail}",
        params=request.url.query or None,
        content=content,
        headers=headers,
    )

    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        _finish(request_id, method, tail, 502, started, requested_model)
        return _error_json("upstream_unreachable", f"Could not reach OpenRouter: {exc}", 502)

    if upstream_response.status_code in (401, 403):
        # Bad OPENROUTER_API_KEY, not a client mistake (spec §5).
        await upstream_response.aclose()
        _finish(request_id, method, tail, 502, started, requested_model)
        return JSONResponse(
            upstream_auth_error_body(upstream_response.status_code, settings), status_code=502
        )

    status = upstream_response.status_code

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            _finish(request_id, method, tail, status, started, requested_model)

    return StreamingResponse(
        body_iter(),
        status_code=status,
        headers=_filter_response_headers(upstream_response.headers),
    )
