from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


def require_proxy_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "missing_authorization",
                    "message": "Authorization header required",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "invalid_authorization", "message": "Bearer token required"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token != settings.proxy_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "invalid_token", "message": "Invalid proxy API key"}},
        )
