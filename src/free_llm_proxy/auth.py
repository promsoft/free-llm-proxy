from fastapi import Depends, Header, HTTPException, status

from .anthropic_translate import AnthropicError
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


def require_anthropic_key(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Auth for /api/anthropic: accept the proxy key from `x-api-key` (Anthropic
    SDK / Claude Code with ANTHROPIC_API_KEY) or `Authorization: Bearer`
    (ANTHROPIC_AUTH_TOKEN). Errors use the Anthropic error envelope."""
    token = x_api_key
    if not token and authorization:
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() == "bearer" and bearer:
            token = bearer
    if not token:
        raise AnthropicError(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_error",
            "Missing API key: provide x-api-key or Authorization: Bearer.",
        )
    if token != settings.proxy_api_key:
        raise AnthropicError(
            status.HTTP_401_UNAUTHORIZED, "authentication_error", "Invalid API key."
        )
