from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_proxy_key
from ..deps import get_registry
from ..registry import ModelRegistry

router = APIRouter(prefix="/v1", tags=["models"], dependencies=[Depends(require_proxy_key)])


@router.get("/models")
async def list_models(registry: ModelRegistry = Depends(get_registry)) -> dict:
    snap = registry.snapshot
    if snap is None or not snap.models:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "not_ready", "message": "Model snapshot not available yet"}},
        )
    created = int(snap.fetched_at.timestamp())
    return {
        "object": "list",
        "data": [
            {"id": m.id, "object": "model", "created": created, "owned_by": "openrouter"}
            for m in snap.models
        ],
    }
