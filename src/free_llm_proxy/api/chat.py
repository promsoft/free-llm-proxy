import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk

from ..auth import require_proxy_key
from ..config import Settings, get_settings
from ..deps import get_registry
from ..fallback import AttemptSuccess, apply_cooldown, record_attempt, run_fallback
from ..logging import get_logger
from ..metrics import request_duration_seconds, requests_total
from ..registry import Cooldowns, ModelRegistry
from ..router import select_candidates
from ..upstream import Outcome, Upstream, UpstreamError, classify_exception

router = APIRouter(prefix="/v1", tags=["chat"], dependencies=[Depends(require_proxy_key)])
log = get_logger(__name__)


def _err(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message}},
    )


def _passthrough_client_error(exc: UpstreamError) -> JSONResponse:
    status = exc.status_code or 502
    body = exc.body or {"error": {"message": exc.message}}
    if isinstance(body, dict) and "error" not in body:
        body = {"error": body}
    return JSONResponse(body, status_code=status)


def _key_tail(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 4:
        return f"len={len(key)}"
    return f"...{key[-4:]} (len={len(key)})"


def _upstream_auth_error_response(exc: UpstreamError, settings: Settings) -> JSONResponse:
    """401/403 from upstream means our OPENROUTER_API_KEY is bad. Return 502."""
    return JSONResponse(
        {
            "error": {
                "code": "upstream_auth_error",
                "message": (
                    f"Proxy could not authenticate with upstream "
                    f"(HTTP {exc.status_code}). Check OPENROUTER_API_KEY: "
                    f"current key tail is {_key_tail(settings.openrouter_api_key)}."
                ),
                "type": "proxy_misconfiguration",
            }
        },
        status_code=502,
    )


def _terminal_error_response(
    exc: UpstreamError,
    request_id: str,
    total_seconds: float,
    attempts: list[dict[str, Any]],
    body: dict,
    settings: Settings,
    *,
    stream: bool,
) -> JSONResponse:
    if exc.outcome is Outcome.UPSTREAM_AUTH_ERROR:
        response = _upstream_auth_error_response(exc, settings)
        log_status = 502
        log.error(
            "request_done",
            extra={
                "request_id": request_id,
                "duration_ms": int(total_seconds * 1000),
                "status": log_status,
                "chosen_model": None,
                "stream": stream,
                "attempts": attempts,
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
                "reason": "upstream_auth_error",
                "key_tail": _key_tail(settings.openrouter_api_key),
            },
        )
    else:
        response = _passthrough_client_error(exc)
        log_status = exc.status_code or 502
        log.info(
            "request_done",
            extra={
                "request_id": request_id,
                "duration_ms": int(total_seconds * 1000),
                "status": log_status,
                "chosen_model": None,
                "stream": stream,
                "attempts": attempts,
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
            },
        )
    requests_total.labels(str(log_status)).inc()
    return response


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    registry: ModelRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> Any:
    try:
        body = await request.json()
    except ValueError as exc:
        log.info("request_rejected", extra={"reason": "invalid_json", "error": str(exc)})
        raise _err("invalid_json", "Request body is not valid JSON.", 400) from exc

    is_stream = bool(body.get("stream"))

    snap = registry.snapshot
    if snap is None or not snap.models:
        log.info("request_rejected", extra={"reason": "not_ready"})
        raise _err("not_ready", "Model snapshot is not available yet.", 503)

    now = datetime.now(UTC)
    candidates = select_candidates(snap.models, body, registry.cooldowns, now)
    if not candidates:
        log.info(
            "request_rejected",
            extra={
                "reason": "no_capable_model",
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
                "had_seed": body.get("seed") is not None,
                "had_stop": body.get("stop") is not None,
                "snapshot_size": len(snap.models),
            },
        )
        raise _err(
            "no_capable_model",
            "No model in current snapshot supports the requested capabilities.",
            400,
        )

    upstream: Upstream = request.app.state.upstream
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    capped = candidates[: settings.max_fallback_attempts]

    if is_stream:
        return await _handle_stream(
            request, upstream, registry, settings, body, capped, request_id, started
        )
    return await _handle_nonstream(upstream, registry, settings, body, capped, request_id, started)


async def _handle_nonstream(
    upstream: Upstream,
    registry: ModelRegistry,
    settings: Settings,
    body: dict,
    candidates: list,
    request_id: str,
    started: float,
):
    outcome = await run_fallback(
        candidates,
        lambda model_id: upstream.chat(model_id, body),
        cooldowns=registry.cooldowns,
        settings=settings,
    )

    if isinstance(outcome, AttemptSuccess):
        model = outcome.model
        duration_ms = int((time.perf_counter() - outcome.attempt_started) * 1000)
        record_attempt(
            outcome.attempts, model_id=model.id, outcome=Outcome.SUCCESS, duration_ms=duration_ms
        )
        total = time.perf_counter() - started
        request_duration_seconds.observe(total)
        requests_total.labels("200").inc()
        log.info(
            "request_done",
            extra={
                "request_id": request_id,
                "duration_ms": int(total * 1000),
                "status": 200,
                "chosen_model": model.id,
                "stream": False,
                "attempts": outcome.attempts,
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
            },
        )
        return JSONResponse(outcome.result, headers={"x-free-llm-proxy-model": model.id})

    total = time.perf_counter() - started
    request_duration_seconds.observe(total)

    if outcome.terminal_error is not None:
        return _terminal_error_response(
            outcome.terminal_error,
            request_id,
            total,
            outcome.attempts,
            body,
            settings,
            stream=False,
        )

    requests_total.labels("503").inc()
    log.warning(
        "request_done",
        extra={
            "request_id": request_id,
            "duration_ms": int(total * 1000),
            "status": 503,
            "chosen_model": None,
            "stream": False,
            "attempts": outcome.attempts,
            "had_tools": bool(body.get("tools")),
            "had_response_format": bool(body.get("response_format")),
        },
    )
    raise _err("all_models_unavailable", "All candidate models failed; try again later.", 503)


async def _handle_stream(
    request: Request,
    upstream: Upstream,
    registry: ModelRegistry,
    settings: Settings,
    body: dict,
    candidates: list,
    request_id: str,
    started: float,
):
    outcome = await run_fallback(
        candidates,
        lambda model_id: upstream.chat_stream(model_id, body),
        cooldowns=registry.cooldowns,
        settings=settings,
    )

    if isinstance(outcome, AttemptSuccess):
        model = outcome.model
        # We have a live SSE connection — commit and return StreamingResponse.
        return StreamingResponse(
            _emit_sse(
                outcome.result,
                model_id=model.id,
                attempts=outcome.attempts,
                attempt_started=outcome.attempt_started,
                request_id=request_id,
                started=started,
                body=body,
                cooldowns=registry.cooldowns,
                settings=settings,
            ),
            media_type="text/event-stream",
            headers={
                "x-free-llm-proxy-model": model.id,
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    total = time.perf_counter() - started
    request_duration_seconds.observe(total)

    if outcome.terminal_error is not None:
        return _terminal_error_response(
            outcome.terminal_error, request_id, total, outcome.attempts, body, settings, stream=True
        )

    requests_total.labels("503").inc()
    log.warning(
        "request_done",
        extra={
            "request_id": request_id,
            "duration_ms": int(total * 1000),
            "status": 503,
            "chosen_model": None,
            "stream": True,
            "attempts": outcome.attempts,
            "had_tools": bool(body.get("tools")),
            "had_response_format": bool(body.get("response_format")),
        },
    )
    raise _err("all_models_unavailable", "All candidate models failed; try again later.", 503)


def _sse_data(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


SSE_DONE = b"data: [DONE]\n\n"


async def _emit_sse(
    stream: AsyncStream[ChatCompletionChunk],
    *,
    model_id: str,
    attempts: list[dict[str, Any]],
    attempt_started: float,
    request_id: str,
    started: float,
    body: dict,
    cooldowns: Cooldowns,
    settings: Settings,
) -> AsyncIterator[bytes]:
    """Serialize chunks as SSE; on mid-stream error, emit error event then [DONE]."""
    mid_error: UpstreamError | None = None
    chunks_emitted = 0
    try:
        async for chunk in stream:
            chunks_emitted += 1
            yield _sse_data(chunk.model_dump())
    except Exception as exc:
        mid_error = classify_exception(exc) or UpstreamError(
            Outcome.UPSTREAM_ERROR, status_code=None, message=f"stream error: {exc}"
        )
        # No fallback once chunks have started — emit error event in-band.
        # Still mark cooldown so the model gets a rest on the next request.
        apply_cooldown(cooldowns, model_id, mid_error, settings)
        yield _sse_data(
            {
                "error": {
                    "message": mid_error.message,
                    "code": mid_error.outcome.value,
                    "type": "upstream_error",
                }
            }
        )
    finally:
        with contextlib.suppress(Exception):
            await stream.close()

    yield SSE_DONE

    duration_ms = int((time.perf_counter() - attempt_started) * 1000)
    outcome = Outcome.SUCCESS if mid_error is None else mid_error.outcome
    record_attempt(
        attempts,
        model_id=model_id,
        outcome=outcome,
        duration_ms=duration_ms,
        status_code=mid_error.status_code if mid_error else None,
    )
    total = time.perf_counter() - started
    request_duration_seconds.observe(total)
    status_label = "200" if mid_error is None else "200/mid_error"
    requests_total.labels(status_label).inc()
    log_level = log.info if mid_error is None else log.warning
    log_level(
        "request_done",
        extra={
            "request_id": request_id,
            "duration_ms": int(total * 1000),
            "status": 200,
            "chosen_model": model_id,
            "stream": True,
            "chunks_emitted": chunks_emitted,
            "mid_stream_error": mid_error.outcome.value if mid_error else None,
            "attempts": attempts,
            "had_tools": bool(body.get("tools")),
            "had_response_format": bool(body.get("response_format")),
        },
    )
