"""Anthropic Messages API shim, mounted under /api/anthropic (see spec/anthropic.md).

Translates Anthropic requests into OpenAI Chat Completions, reuses the shared
fallback loop against OpenRouter, and translates responses (and SSE streams)
back into Anthropic shape. Errors are rendered via `AnthropicError`, which the
app-level exception handler serializes into Anthropic's error envelope.
"""

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..anthropic_translate import (
    AnthropicError,
    AnthropicStreamTranslator,
    anthropic_to_openai_request,
    estimate_input_tokens,
    openai_to_anthropic_response,
)
from ..auth import require_anthropic_key
from ..config import Settings, get_settings
from ..deps import get_registry
from ..fallback import AttemptSuccess, apply_cooldown, record_attempt, run_fallback
from ..logging import get_logger
from ..metrics import request_duration_seconds, requests_total
from ..registry import Cooldowns, ModelRegistry
from ..router import select_candidates
from ..upstream import (
    Outcome,
    Upstream,
    UpstreamError,
    classify_exception,
    key_tail,
    upstream_auth_error_message,
)

router = APIRouter(prefix="/v1", tags=["anthropic"], dependencies=[Depends(require_anthropic_key)])
log = get_logger(__name__)


def _anthropic_type_for_status(status_code: int) -> str:
    by_status = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        409: "invalid_request_error",
        422: "invalid_request_error",
        429: "rate_limit_error",
    }
    if status_code in by_status:
        return by_status[status_code]
    return "api_error" if status_code >= 500 else "invalid_request_error"


def _upstream_message(exc: UpstreamError) -> str:
    body = exc.body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    return exc.message


def _log_done(
    request_id: str,
    total: float,
    status: int,
    chosen_model: str | None,
    *,
    stream: bool,
    attempts: list[dict[str, Any]],
    anthropic_body: dict,
    level: str = "info",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "duration_ms": int(total * 1000),
        "status": status,
        "chosen_model": chosen_model,
        "stream": stream,
        "api": "anthropic",
        "attempts": attempts,
        "had_tools": bool(anthropic_body.get("tools")),
    }
    if extra:
        payload.update(extra)
    getattr(log, level)("request_done", extra=payload)


def _terminal_error_response(
    exc: UpstreamError,
    request_id: str,
    total: float,
    attempts: list[dict[str, Any]],
    anthropic_body: dict,
    settings: Settings,
    *,
    stream: bool,
) -> JSONResponse:
    if exc.outcome is Outcome.UPSTREAM_AUTH_ERROR:
        status_code = 502
        err = AnthropicError(
            status_code, "api_error", upstream_auth_error_message(exc.status_code, settings)
        )
        _log_done(
            request_id,
            total,
            status_code,
            None,
            stream=stream,
            attempts=attempts,
            anthropic_body=anthropic_body,
            level="error",
            extra={
                "reason": "upstream_auth_error",
                "key_tail": key_tail(settings.openrouter_api_key),
            },
        )
    else:
        status_code = exc.status_code or 502
        err = AnthropicError(
            status_code, _anthropic_type_for_status(status_code), _upstream_message(exc)
        )
        _log_done(
            request_id,
            total,
            status_code,
            None,
            stream=stream,
            attempts=attempts,
            anthropic_body=anthropic_body,
        )
    requests_total.labels(str(status_code)).inc()
    return JSONResponse(err.body(), status_code=status_code)


@router.post("/messages")
async def messages(
    request: Request,
    registry: ModelRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> Any:
    try:
        body = await request.json()
    except ValueError as exc:
        raise AnthropicError(
            400, "invalid_request_error", "Request body is not valid JSON."
        ) from exc

    is_stream = bool(body.get("stream"))
    openai_body = anthropic_to_openai_request(body)  # raises AnthropicError(400) on bad input

    snap = registry.snapshot
    if snap is None or not snap.models:
        raise AnthropicError(503, "overloaded_error", "Model snapshot is not available yet.")

    now = datetime.now(UTC)
    candidates = select_candidates(snap.models, openai_body, registry.cooldowns, now)
    if not candidates:
        raise AnthropicError(
            400,
            "invalid_request_error",
            "No model in current snapshot supports the requested capabilities.",
        )

    upstream: Upstream = request.app.state.upstream
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    capped = candidates[: settings.max_fallback_attempts]

    if is_stream:
        return await _handle_stream(
            upstream, registry, settings, body, openai_body, capped, request_id, started
        )
    return await _handle_nonstream(
        upstream, registry, settings, body, openai_body, capped, request_id, started
    )


@router.post("/messages/count_tokens")
async def count_tokens(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise AnthropicError(
            400, "invalid_request_error", "Request body is not valid JSON."
        ) from exc
    if not isinstance(body, dict):
        raise AnthropicError(400, "invalid_request_error", "Request body must be a JSON object.")
    tokens = estimate_input_tokens(body, settings.anthropic_tokens_per_char_divisor)
    return {"input_tokens": tokens}


async def _handle_nonstream(
    upstream: Upstream,
    registry: ModelRegistry,
    settings: Settings,
    anthropic_body: dict,
    openai_body: dict,
    candidates: list,
    request_id: str,
    started: float,
):
    outcome = await run_fallback(
        candidates,
        lambda model_id: upstream.chat(model_id, openai_body),
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
        anthropic_resp = openai_to_anthropic_response(outcome.result, model.id)
        _log_done(
            request_id,
            total,
            200,
            model.id,
            stream=False,
            attempts=outcome.attempts,
            anthropic_body=anthropic_body,
        )
        return JSONResponse(anthropic_resp, headers={"x-free-llm-proxy-model": model.id})

    total = time.perf_counter() - started
    request_duration_seconds.observe(total)
    if outcome.terminal_error is not None:
        return _terminal_error_response(
            outcome.terminal_error,
            request_id,
            total,
            outcome.attempts,
            anthropic_body,
            settings,
            stream=False,
        )
    requests_total.labels("503").inc()
    _log_done(
        request_id,
        total,
        503,
        None,
        stream=False,
        attempts=outcome.attempts,
        anthropic_body=anthropic_body,
        level="warning",
    )
    raise AnthropicError(503, "overloaded_error", "All candidate models failed; try again later.")


async def _handle_stream(
    upstream: Upstream,
    registry: ModelRegistry,
    settings: Settings,
    anthropic_body: dict,
    openai_body: dict,
    candidates: list,
    request_id: str,
    started: float,
):
    stream_body = {**openai_body, "stream_options": {"include_usage": True}}
    outcome = await run_fallback(
        candidates,
        lambda model_id: upstream.chat_stream(model_id, stream_body),
        cooldowns=registry.cooldowns,
        settings=settings,
    )

    if isinstance(outcome, AttemptSuccess):
        model = outcome.model
        input_tokens = estimate_input_tokens(
            anthropic_body, settings.anthropic_tokens_per_char_divisor
        )
        return StreamingResponse(
            _emit_anthropic_sse(
                outcome.result,
                model_id=model.id,
                input_tokens=input_tokens,
                attempts=outcome.attempts,
                attempt_started=outcome.attempt_started,
                request_id=request_id,
                started=started,
                anthropic_body=anthropic_body,
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
            outcome.terminal_error,
            request_id,
            total,
            outcome.attempts,
            anthropic_body,
            settings,
            stream=True,
        )
    requests_total.labels("503").inc()
    _log_done(
        request_id,
        total,
        503,
        None,
        stream=True,
        attempts=outcome.attempts,
        anthropic_body=anthropic_body,
        level="warning",
    )
    raise AnthropicError(503, "overloaded_error", "All candidate models failed; try again later.")


def _sse_event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _emit_anthropic_sse(
    stream,
    *,
    model_id: str,
    input_tokens: int,
    attempts: list[dict[str, Any]],
    attempt_started: float,
    request_id: str,
    started: float,
    anthropic_body: dict,
    cooldowns: Cooldowns,
    settings: Settings,
) -> AsyncIterator[bytes]:
    """Drive the Anthropic event sequence; on mid-stream error emit `event: error`."""
    translator = AnthropicStreamTranslator(model_id, input_tokens=input_tokens)
    mid_error: UpstreamError | None = None
    chunks_emitted = 0

    for name, data in translator.start():
        yield _sse_event(name, data)
    try:
        async for chunk in stream:
            chunks_emitted += 1
            for name, data in translator.feed(chunk.model_dump()):
                yield _sse_event(name, data)
    except Exception as exc:
        mid_error = classify_exception(exc) or UpstreamError(
            Outcome.UPSTREAM_ERROR, status_code=None, message=f"stream error: {exc}"
        )
        apply_cooldown(cooldowns, model_id, mid_error, settings)
        err_type = "overloaded_error" if mid_error.outcome is Outcome.RATE_LIMITED else "api_error"
        name, data = translator.error_event(err_type, mid_error.message)
        yield _sse_event(name, data)
    finally:
        with contextlib.suppress(Exception):
            await stream.close()

    if mid_error is None:
        for name, data in translator.finish():
            yield _sse_event(name, data)

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
    requests_total.labels("200" if mid_error is None else "200/mid_error").inc()
    _log_done(
        request_id,
        total,
        200,
        model_id,
        stream=True,
        attempts=attempts,
        anthropic_body=anthropic_body,
        level="info" if mid_error is None else "warning",
        extra={
            "chunks_emitted": chunks_emitted,
            "mid_stream_error": mid_error.outcome.value if mid_error else None,
        },
    )
