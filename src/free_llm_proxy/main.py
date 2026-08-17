from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .anthropic_translate import AnthropicError
from .api import admin, anthropic, chat, models_endpoint, openrouter, ops
from .config import Settings, get_settings
from .logging import configure_logging
from .refresher import Refresher
from .registry import ModelRegistry
from .upstream import Upstream


async def _anthropic_error_handler(request: Request, exc: AnthropicError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status_code)


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresher: Refresher = app.state.refresher
    upstream: Upstream = app.state.upstream
    if getattr(app.state, "auto_start_refresher", True):
        refresher.start()
    try:
        yield
    finally:
        await refresher.stop()
        await upstream.aclose()
        await app.state.openrouter_proxy_client.aclose()


def create_app(settings: Settings | None = None, *, auto_start_refresher: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="free-llm-proxy", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = ModelRegistry()
    app.state.refresher = Refresher(app.state.registry, settings)
    app.state.upstream = Upstream(settings)
    app.state.auto_start_refresher = auto_start_refresher

    app.include_router(ops.router)
    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(models_endpoint.router)
    # OpenRouter-style alias: many OpenAI-compatible clients append /api/v1.
    app.include_router(chat.router, prefix="/api")
    app.include_router(models_endpoint.router, prefix="/api")
    # Anthropic Messages API shim (for Claude Code / anthropic SDK).
    if settings.anthropic_api_enabled:
        app.include_router(anthropic.router, prefix="/api/anthropic")
        app.add_exception_handler(AnthropicError, _anthropic_error_handler)
    # OpenRouter passthrough: client-named model, raw byte relay.
    app.state.openrouter_proxy_client = httpx.AsyncClient(
        base_url=settings.openrouter_proxy_base,
        timeout=settings.openrouter_proxy_timeout_sec,
    )
    if settings.openrouter_proxy_enabled:
        app.include_router(openrouter.router, prefix="/api/openrouter")
    return app


app = create_app()
