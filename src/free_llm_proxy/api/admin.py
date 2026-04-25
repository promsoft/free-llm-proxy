from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_proxy_key
from ..deps import get_refresher, get_registry
from ..refresher import Refresher
from ..registry import ModelRegistry

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_proxy_key)])


@router.post("/refresh")
async def refresh(
    refresher: Refresher = Depends(get_refresher),
    registry: ModelRegistry = Depends(get_registry),
) -> dict:
    try:
        count = await refresher.fetch_once()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": {"code": "refresh_failed", "message": str(exc)}},
        ) from exc
    registry.cooldowns.reset()
    snap = registry.snapshot
    return {
        "models": count,
        "fetched_at": snap.fetched_at.isoformat() if snap else None,
    }
