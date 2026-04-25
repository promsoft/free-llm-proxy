import asyncio
import contextlib

import httpx

from .config import Settings
from .logging import get_logger
from .models import TopModelsResponse
from .registry import ModelRegistry

log = get_logger(__name__)


class Refresher:
    def __init__(self, registry: ModelRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None

    async def fetch_once(self) -> int:
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            r = await client.get(self._settings.models_list_url)
            r.raise_for_status()
            parsed = TopModelsResponse.model_validate(r.json())
        finally:
            if self._client is None:
                await client.aclose()
        await self._registry.replace_snapshot(parsed.models)
        log.info(
            "snapshot_refreshed",
            extra={"count": len(parsed.models), "url": self._settings.models_list_url},
        )
        return len(parsed.models)

    async def _loop(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)
        try:
            while not self._stop.is_set():
                try:
                    await self.fetch_once()
                except Exception as exc:
                    log.warning(
                        "snapshot_refresh_failed",
                        extra={"error": str(exc), "type": type(exc).__name__},
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._settings.models_refresh_sec
                    )
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="refresher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
