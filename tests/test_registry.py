from datetime import UTC, datetime, timedelta

import pytest

from free_llm_proxy.models import Model
from free_llm_proxy.registry import Cooldowns, ModelRegistry


def make_model(rank: int, mid: str, **kw) -> Model:
    return Model(rank=rank, id=mid, **kw)


def test_cooldown_set_and_check():
    cd = Cooldowns()
    now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
    cd.mark("m1", now + timedelta(seconds=30))
    assert cd.is_cooled_down("m1", now)
    assert not cd.is_cooled_down("m1", now + timedelta(seconds=31))
    assert not cd.is_cooled_down("other", now)


def test_cooldown_takes_max_when_set_twice():
    cd = Cooldowns()
    now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
    cd.mark("m1", now + timedelta(seconds=10))
    cd.mark("m1", now + timedelta(seconds=60))
    assert cd.until["m1"] == now + timedelta(seconds=60)
    cd.mark("m1", now + timedelta(seconds=5))  # shorter — ignored
    assert cd.until["m1"] == now + timedelta(seconds=60)


def test_cooldown_cleanup_drops_expired_and_unknown():
    cd = Cooldowns()
    now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
    cd.mark("alive", now + timedelta(seconds=60))
    cd.mark("expired", now - timedelta(seconds=1))
    cd.mark("removed", now + timedelta(seconds=60))
    cd.cleanup(now, known_ids={"alive", "expired"})
    assert "alive" in cd.until
    assert "expired" not in cd.until
    assert "removed" not in cd.until


@pytest.mark.asyncio
async def test_registry_replace_snapshot_sorts_by_rank():
    reg = ModelRegistry()
    await reg.replace_snapshot([make_model(3, "c"), make_model(1, "a"), make_model(2, "b")])
    snap = reg.snapshot
    assert snap is not None
    assert [m.id for m in snap.models] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_registry_replace_cleans_cooldowns_for_dropped_models():
    reg = ModelRegistry()
    await reg.replace_snapshot([make_model(1, "a"), make_model(2, "b")])
    reg.cooldowns.mark("a", datetime.now(UTC) + timedelta(seconds=600))
    reg.cooldowns.mark("b", datetime.now(UTC) + timedelta(seconds=600))
    await reg.replace_snapshot([make_model(1, "a")])  # b dropped
    assert "a" in reg.cooldowns.until
    assert "b" not in reg.cooldowns.until


@pytest.mark.asyncio
async def test_has_available_model_false_when_all_cooled_down():
    reg = ModelRegistry()
    await reg.replace_snapshot([make_model(1, "a"), make_model(2, "b")])
    reg.cooldowns.mark("a", datetime.now(UTC) + timedelta(seconds=600))
    reg.cooldowns.mark("b", datetime.now(UTC) + timedelta(seconds=600))
    assert not reg.has_available_model()


@pytest.mark.asyncio
async def test_has_available_model_true_when_one_free():
    reg = ModelRegistry()
    await reg.replace_snapshot([make_model(1, "a"), make_model(2, "b")])
    reg.cooldowns.mark("a", datetime.now(UTC) + timedelta(seconds=600))
    assert reg.has_available_model()


@pytest.mark.asyncio
async def test_empty_snapshot_not_ready():
    reg = ModelRegistry()
    assert not reg.has_available_model()
    await reg.replace_snapshot([])
    assert not reg.has_available_model()
