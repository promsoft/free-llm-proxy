"""Shared upstream attempt loop: candidate iteration, cooldown, attempt log.

Format-agnostic core reused by every public endpoint (OpenAI chat, Anthropic
messages). It knows how to try candidates in rank order, classify failures into
cooldown-vs-terminal, record per-attempt telemetry, and hand back either the
first success or an exhaustion result. It does *not* know how the success or the
error is serialized to the client — that stays in the endpoint.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .metrics import upstream_attempts_total
from .models import Model
from .registry import Cooldowns
from .upstream import Outcome, UpstreamError

# Outcomes that must NOT trigger fallback: surface them to the client as-is.
NO_FALLBACK_OUTCOMES = frozenset({Outcome.CLIENT_ERROR, Outcome.UPSTREAM_AUTH_ERROR})


def cooldown_until(exc: UpstreamError, settings: Settings) -> datetime | None:
    """When the failed model should be retryable again, or None if no cooldown."""
    if exc.outcome is Outcome.RATE_LIMITED:
        return exc.retry_after or (
            datetime.now(UTC) + timedelta(seconds=settings.rate_limit_cooldown_sec)
        )
    if exc.outcome is Outcome.UPSTREAM_ERROR:
        return exc.retry_after or (
            datetime.now(UTC) + timedelta(seconds=settings.generic_error_cooldown_sec)
        )
    return None


def apply_cooldown(
    cooldowns: Cooldowns, model_id: str, exc: UpstreamError, settings: Settings
) -> datetime | None:
    until = cooldown_until(exc, settings)
    if until is not None:
        cooldowns.mark(model_id, until)
    return until


def record_attempt(
    attempts: list[dict[str, Any]],
    *,
    model_id: str,
    outcome: Outcome,
    duration_ms: int,
    status_code: int | None = None,
    cooldown_until: datetime | None = None,
) -> None:
    upstream_attempts_total.labels(model_id, outcome.value).inc()
    entry: dict[str, Any] = {
        "model": model_id,
        "outcome": outcome.value,
        "duration_ms": duration_ms,
    }
    if status_code is not None:
        entry["status"] = status_code
    if cooldown_until is not None:
        entry["cooldown_until"] = cooldown_until.isoformat()
    attempts.append(entry)


@dataclass
class AttemptSuccess:
    """A candidate accepted the call. `result` is the raw upstream return value
    (a completion dict for non-stream, an open AsyncStream for stream)."""

    model: Model
    result: Any
    attempt_started: float
    attempts: list[dict[str, Any]]


@dataclass
class AttemptExhausted:
    """No candidate succeeded. `terminal_error` set → a no-fallback error stopped
    the loop early; None → every candidate was tried and failed (503)."""

    attempts: list[dict[str, Any]]
    terminal_error: UpstreamError | None


async def run_fallback(
    candidates: list[Model],
    call: Callable[[str], Awaitable[Any]],
    *,
    cooldowns: Cooldowns,
    settings: Settings,
) -> AttemptSuccess | AttemptExhausted:
    """Try `call(model_id)` for each candidate until one succeeds.

    On `UpstreamError`: a NO_FALLBACK outcome stops immediately
    (`AttemptExhausted` with `terminal_error`); anything else marks a cooldown
    and moves on. Success attempts are *not* recorded here — the caller records
    them, because non-stream and stream measure success differently.
    """
    attempts: list[dict[str, Any]] = []
    for model in candidates:
        attempt_started = time.perf_counter()
        try:
            result = await call(model.id)
        except UpstreamError as exc:
            duration_ms = int((time.perf_counter() - attempt_started) * 1000)
            if exc.outcome in NO_FALLBACK_OUTCOMES:
                record_attempt(
                    attempts,
                    model_id=model.id,
                    outcome=exc.outcome,
                    duration_ms=duration_ms,
                    status_code=exc.status_code,
                )
                return AttemptExhausted(attempts, terminal_error=exc)
            until = apply_cooldown(cooldowns, model.id, exc, settings)
            record_attempt(
                attempts,
                model_id=model.id,
                outcome=exc.outcome,
                duration_ms=duration_ms,
                status_code=exc.status_code,
                cooldown_until=until,
            )
            continue
        return AttemptSuccess(model, result, attempt_started, attempts)
    return AttemptExhausted(attempts, terminal_error=None)
