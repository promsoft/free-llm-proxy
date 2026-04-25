import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Model


@dataclass
class Snapshot:
    models: list[Model]
    fetched_at: datetime

    def by_id(self) -> dict[str, Model]:
        return {m.id: m for m in self.models}


@dataclass
class Cooldowns:
    until: dict[str, datetime] = field(default_factory=dict)

    def mark(self, model_id: str, until: datetime) -> None:
        existing = self.until.get(model_id)
        if existing is None or until > existing:
            self.until[model_id] = until

    def is_cooled_down(self, model_id: str, now: datetime) -> bool:
        u = self.until.get(model_id)
        return u is not None and u > now

    def cleanup(self, now: datetime, known_ids: set[str] | None = None) -> None:
        self.until = {
            mid: u
            for mid, u in self.until.items()
            if u > now and (known_ids is None or mid in known_ids)
        }

    def reset(self) -> None:
        self.until.clear()


class ModelRegistry:
    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None
        self._cooldowns = Cooldowns()
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    @property
    def cooldowns(self) -> Cooldowns:
        return self._cooldowns

    async def replace_snapshot(self, models: list[Model]) -> None:
        async with self._lock:
            self._snapshot = Snapshot(
                models=sorted(models, key=lambda m: m.rank),
                fetched_at=datetime.now(UTC),
            )
            self._cooldowns.cleanup(datetime.now(UTC), known_ids={m.id for m in models})

    def has_available_model(self) -> bool:
        if self._snapshot is None or not self._snapshot.models:
            return False
        now = datetime.now(UTC)
        return any(not self._cooldowns.is_cooled_down(m.id, now) for m in self._snapshot.models)
