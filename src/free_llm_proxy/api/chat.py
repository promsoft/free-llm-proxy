import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import require_proxy_key
from ..config import Settings, get_settings
from ..deps import get_registry
from ..logging import get_logger
from ..metrics import request_duration_seconds, requests_total, upstream_attempts_total
from ..registry import ModelRegistry
from ..router import select_candidates
from ..upstream import Outcome, Upstream, UpstreamError

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

    if body.get("stream"):
        log.info(
            "request_rejected",
            extra={
                "reason": "streaming_not_supported",
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
            },
        )
        raise _err(
            "streaming_not_supported",
            "Streaming is not supported by this proxy in MVP. Use stream=false.",
            400,
        )

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
    attempts: list[dict[str, Any]] = []
    started = time.perf_counter()
    last_client_error: UpstreamError | None = None

    for model in candidates[: settings.max_fallback_attempts]:
        attempt_started = time.perf_counter()
        try:
            result = await upstream.chat(model.id, body)
        except UpstreamError as exc:
            duration_ms = int((time.perf_counter() - attempt_started) * 1000)

            upstream_attempts_total.labels(model.id, exc.outcome.value).inc()

            if exc.outcome is Outcome.CLIENT_ERROR:
                attempts.append(
                    {
                        "model": model.id,
                        "outcome": exc.outcome.value,
                        "status": exc.status_code,
                        "duration_ms": duration_ms,
                    }
                )
                last_client_error = exc
                break

            cooldown_until: datetime | None = None
            if exc.outcome is Outcome.RATE_LIMITED:
                cooldown_until = exc.retry_after or (
                    datetime.now(UTC) + timedelta(seconds=settings.rate_limit_cooldown_sec)
                )
            elif exc.outcome is Outcome.UPSTREAM_ERROR:
                cooldown_until = exc.retry_after or (
                    datetime.now(UTC) + timedelta(seconds=settings.generic_error_cooldown_sec)
                )
            if cooldown_until is not None:
                registry.cooldowns.mark(model.id, cooldown_until)

            attempts.append(
                {
                    "model": model.id,
                    "outcome": exc.outcome.value,
                    "status": exc.status_code,
                    "duration_ms": duration_ms,
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                }
            )
            continue

        duration_ms = int((time.perf_counter() - attempt_started) * 1000)
        attempts.append(
            {
                "model": model.id,
                "outcome": Outcome.SUCCESS.value,
                "duration_ms": duration_ms,
            }
        )
        upstream_attempts_total.labels(model.id, Outcome.SUCCESS.value).inc()
        total_duration = time.perf_counter() - started
        request_duration_seconds.observe(total_duration)
        requests_total.labels("200").inc()
        log.info(
            "request_done",
            extra={
                "request_id": request_id,
                "duration_ms": int(total_duration * 1000),
                "status": 200,
                "chosen_model": model.id,
                "attempts": attempts,
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
            },
        )
        return JSONResponse(result, headers={"x-free-llm-proxy-model": model.id})

    total_duration = time.perf_counter() - started
    request_duration_seconds.observe(total_duration)

    if last_client_error is not None:
        status = last_client_error.status_code or 502
        requests_total.labels(str(status)).inc()
        log.info(
            "request_done",
            extra={
                "request_id": request_id,
                "duration_ms": int(total_duration * 1000),
                "status": status,
                "chosen_model": None,
                "attempts": attempts,
                "had_tools": bool(body.get("tools")),
                "had_response_format": bool(body.get("response_format")),
            },
        )
        return _passthrough_client_error(last_client_error)

    requests_total.labels("503").inc()
    log.warning(
        "request_done",
        extra={
            "request_id": request_id,
            "duration_ms": int(total_duration * 1000),
            "status": 503,
            "chosen_model": None,
            "attempts": attempts,
            "had_tools": bool(body.get("tools")),
            "had_response_format": bool(body.get("response_format")),
        },
    )
    raise _err(
        "all_models_unavailable",
        "All candidate models failed; try again later.",
        503,
    )
