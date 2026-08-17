import httpx
from fastapi import Request

from .refresher import Refresher
from .registry import ModelRegistry


def get_registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


def get_refresher(request: Request) -> Refresher:
    return request.app.state.refresher


def get_openrouter_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.openrouter_proxy_client
