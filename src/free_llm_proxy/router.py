from datetime import datetime
from typing import Any

from .models import Model
from .registry import Cooldowns


def _required_capabilities(request: dict[str, Any]) -> list[str]:
    """Return list of attribute names on Model that must be True for this request."""
    needed: list[str] = []

    if request.get("tools"):
        needed.append("supports_tools")

    tool_choice = request.get("tool_choice")
    if tool_choice is not None and tool_choice != "auto":
        needed.append("supports_tool_choice")

    rf = request.get("response_format")
    if isinstance(rf, dict):
        rf_type = rf.get("type")
        if rf_type == "json_schema":
            needed.append("supports_structured_outputs")
        elif rf_type == "json_object":
            needed.append("supports_response_format")

    if request.get("seed") is not None:
        needed.append("supports_seed")

    if request.get("stop") is not None:
        needed.append("supports_stop")

    if request.get("reasoning") is not None or request.get("reasoning_effort") is not None:
        needed.append("supports_reasoning")

    return needed


def select_candidates(
    snapshot_models: list[Model],
    request: dict[str, Any],
    cooldowns: Cooldowns,
    now: datetime,
) -> list[Model]:
    """Pick models suitable for the request, ordered by rank ASC.

    1. Filter by capability flags derived from the request body.
    2. Drop models currently in cooldown.
    """
    needed = _required_capabilities(request)
    candidates: list[Model] = []
    for m in snapshot_models:
        if any(not getattr(m, cap) for cap in needed):
            continue
        if cooldowns.is_cooled_down(m.id, now):
            continue
        candidates.append(m)
    candidates.sort(key=lambda m: m.rank)
    return candidates
