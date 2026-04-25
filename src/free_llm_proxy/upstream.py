from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx
from openai import APIStatusError, APITimeoutError, AsyncOpenAI, AsyncStream, RateLimitError
from openai.types.chat import ChatCompletionChunk

from .config import Settings


class Outcome(StrEnum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"  # cooldown, fallback
    UPSTREAM_ERROR = "upstream_error"  # 5xx/timeout: cooldown, fallback
    CLIENT_ERROR = "client_error"  # 4xx (≠429): no fallback, propagate


class UpstreamError(Exception):
    def __init__(
        self,
        outcome: Outcome,
        status_code: int | None,
        message: str,
        retry_after: datetime | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        self.body = body


def parse_retry_after(headers: httpx.Headers | dict[str, str], now: datetime) -> datetime | None:
    """Return absolute UTC datetime when the resource is available again, or None."""
    h = dict(headers) if not isinstance(headers, httpx.Headers) else headers
    ra = h.get("retry-after") or h.get("Retry-After")
    if ra:
        ra = ra.strip()
        try:
            secs = int(ra)
            return now + timedelta(seconds=max(0, secs))
        except ValueError:
            try:
                dt = parsedate_to_datetime(ra)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt
            except (TypeError, ValueError):
                pass
    reset = h.get("x-ratelimit-reset") or h.get("X-RateLimit-Reset")
    if reset:
        try:
            ts = int(reset)
            if ts > 10**12:  # ms epoch
                ts //= 1000
            return datetime.fromtimestamp(ts, tz=UTC)
        except (ValueError, OSError):
            pass
    return None


def classify_exception(exc: BaseException) -> UpstreamError | None:
    """Map openai SDK / httpx exceptions to UpstreamError. Returns None if unknown."""
    if isinstance(exc, RateLimitError):
        now = datetime.now(UTC)
        retry_after = (
            parse_retry_after(getattr(exc, "response", None).headers, now)
            if getattr(exc, "response", None) is not None
            else None
        )
        return UpstreamError(
            Outcome.RATE_LIMITED,
            status_code=429,
            message=str(exc),
            retry_after=retry_after,
            body=getattr(exc, "body", None),
        )
    if isinstance(exc, APITimeoutError):
        return UpstreamError(
            Outcome.UPSTREAM_ERROR,
            status_code=None,
            message=f"upstream timeout: {exc}",
        )
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        ra: datetime | None = None
        if status == 503 and exc.response is not None:
            ra = parse_retry_after(exc.response.headers, datetime.now(UTC))
        if 500 <= status < 600:
            return UpstreamError(
                Outcome.UPSTREAM_ERROR,
                status_code=status,
                message=str(exc),
                retry_after=ra,
                body=getattr(exc, "body", None),
            )
        return UpstreamError(
            Outcome.CLIENT_ERROR,
            status_code=status,
            message=str(exc),
            body=getattr(exc, "body", None),
        )
    if isinstance(exc, httpx.HTTPError):
        return UpstreamError(
            Outcome.UPSTREAM_ERROR,
            status_code=None,
            message=f"transport error: {exc}",
        )
    return None


class Upstream:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.upstream_base_url,
            timeout=settings.upstream_timeout_sec,
            max_retries=0,
            default_headers={
                "HTTP-Referer": settings.openrouter_referer,
                "X-Title": settings.openrouter_title,
            },
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def chat(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        params = {**body, "model": model_id, "stream": False}
        try:
            resp = await self._client.chat.completions.create(**params)
        except Exception as exc:
            err = classify_exception(exc)
            if err is None:
                raise
            raise err from exc
        return resp.model_dump()

    async def chat_stream(
        self, model_id: str, body: dict[str, Any]
    ) -> AsyncStream[ChatCompletionChunk]:
        """Open a streaming completion. Raises UpstreamError on creation-time failures.

        Iteration on the returned stream may also raise (mid-stream errors); the
        caller is responsible for catching those — use `classify_exception` to map.
        """
        params = {**body, "model": model_id, "stream": True}
        try:
            return await self._client.chat.completions.create(**params)
        except Exception as exc:
            err = classify_exception(exc)
            if err is None:
                raise
            raise err from exc
