from datetime import UTC, datetime, timedelta

import pytest

from free_llm_proxy.models import Model
from free_llm_proxy.registry import Cooldowns
from free_llm_proxy.router import _required_capabilities, select_candidates


def model(rank: int, mid: str, **caps) -> Model:
    return Model(rank=rank, id=mid, **caps)


NOW = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "request_body, expected_caps",
    [
        ({}, []),
        ({"tools": [{"type": "function", "function": {"name": "f"}}]}, ["supports_tools"]),
        ({"tools": []}, []),
        ({"tool_choice": "auto"}, []),
        ({"tool_choice": "required"}, ["supports_tool_choice"]),
        ({"tool_choice": {"type": "function"}}, ["supports_tool_choice"]),
        ({"response_format": {"type": "text"}}, []),
        ({"response_format": {"type": "json_object"}}, ["supports_response_format"]),
        ({"response_format": {"type": "json_schema"}}, ["supports_structured_outputs"]),
        ({"seed": 42}, ["supports_seed"]),
        ({"seed": 0}, ["supports_seed"]),
        ({"stop": ["END"]}, ["supports_stop"]),
        ({"reasoning_effort": "high"}, ["supports_reasoning"]),
        ({"reasoning": {"effort": "high"}}, ["supports_reasoning"]),
        (
            {"tools": [{}], "seed": 1, "stop": ["x"]},
            ["supports_tools", "supports_seed", "supports_stop"],
        ),
    ],
)
def test_required_capabilities(request_body, expected_caps):
    assert _required_capabilities(request_body) == expected_caps


def test_no_filter_returns_all_sorted_by_rank():
    models = [model(3, "c"), model(1, "a"), model(2, "b")]
    out = select_candidates(models, {}, Cooldowns(), NOW)
    assert [m.id for m in out] == ["a", "b", "c"]


def test_capability_filter_removes_unsupported():
    a = model(1, "a", supportsTools=True)
    b = model(2, "b", supportsTools=False)
    c = model(3, "c", supportsTools=True)
    out = select_candidates(
        [a, b, c],
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        Cooldowns(),
        NOW,
    )
    assert [m.id for m in out] == ["a", "c"]


def test_multiple_capabilities_must_all_match():
    a = model(1, "a", supportsTools=True, supportsSeed=True)
    b = model(2, "b", supportsTools=True, supportsSeed=False)
    c = model(3, "c", supportsTools=False, supportsSeed=True)
    out = select_candidates(
        [a, b, c],
        {"tools": [{}], "seed": 1},
        Cooldowns(),
        NOW,
    )
    assert [m.id for m in out] == ["a"]


def test_cooldown_drops_model():
    a = model(1, "a")
    b = model(2, "b")
    cd = Cooldowns()
    cd.mark("a", NOW + timedelta(seconds=60))
    out = select_candidates([a, b], {}, cd, NOW)
    assert [m.id for m in out] == ["b"]


def test_expired_cooldown_does_not_drop():
    a = model(1, "a")
    cd = Cooldowns()
    cd.mark("a", NOW - timedelta(seconds=1))
    out = select_candidates([a], {}, cd, NOW)
    assert [m.id for m in out] == ["a"]


def test_empty_after_filter():
    a = model(1, "a", supportsTools=False)
    out = select_candidates([a], {"tools": [{}]}, Cooldowns(), NOW)
    assert out == []


def test_response_format_json_object_vs_schema():
    a = model(1, "a", supportsResponseFormat=True, supportsStructuredOutputs=False)
    b = model(2, "b", supportsResponseFormat=False, supportsStructuredOutputs=True)
    obj = select_candidates([a, b], {"response_format": {"type": "json_object"}}, Cooldowns(), NOW)
    schema = select_candidates(
        [a, b], {"response_format": {"type": "json_schema"}}, Cooldowns(), NOW
    )
    assert [m.id for m in obj] == ["a"]
    assert [m.id for m in schema] == ["b"]
