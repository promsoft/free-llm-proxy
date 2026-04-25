from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from ..deps import get_registry
from ..metrics import render_latest
from ..registry import ModelRegistry

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(registry: ModelRegistry = Depends(get_registry)) -> Response:
    return Response(content=render_latest(registry), media_type="text/plain; version=0.0.4")


@router.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/ready", include_in_schema=False)
async def ready(registry: ModelRegistry = Depends(get_registry)) -> JSONResponse:
    if registry.has_available_model():
        snap = registry.snapshot
        return JSONResponse(
            {
                "status": "ready",
                "models": len(snap.models) if snap else 0,
                "fetched_at": snap.fetched_at.isoformat() if snap else None,
            }
        )
    return JSONResponse({"status": "not_ready"}, status_code=503)
