from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import admin, chat, models_endpoint, ops
from .config import Settings, get_settings
from .logging import configure_logging
from .refresher import Refresher
from .registry import ModelRegistry
from .upstream import Upstream


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
    return app


app = create_app()
