from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from .registry import ModelRegistry

REGISTRY = CollectorRegistry()

requests_total = Counter(
    "freellm_requests_total",
    "Number of /v1/chat/completions requests by terminal status code.",
    ["status"],
    registry=REGISTRY,
)
request_duration_seconds = Histogram(
    "freellm_request_duration_seconds",
    "End-to-end /v1/chat/completions duration in seconds.",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
upstream_attempts_total = Counter(
    "freellm_upstream_attempts_total",
    "Per-attempt outcomes against upstream (one request can produce several).",
    ["model_id", "outcome"],
    registry=REGISTRY,
)
active_models = Gauge(
    "freellm_active_models",
    "Models in the current snapshot.",
    registry=REGISTRY,
)
cooldown_models = Gauge(
    "freellm_cooldown_models",
    "Models currently in cooldown.",
    registry=REGISTRY,
)
snapshot_age_seconds = Gauge(
    "freellm_snapshot_age_seconds",
    "Seconds since the last successful refresh.",
    registry=REGISTRY,
)


def render_latest(reg: ModelRegistry) -> bytes:
    snap = reg.snapshot
    now = datetime.now(UTC)
    if snap is None:
        active_models.set(0)
        snapshot_age_seconds.set(0)
    else:
        active_models.set(len(snap.models))
        snapshot_age_seconds.set((now - snap.fetched_at).total_seconds())
    cooldown_models.set(sum(1 for u in reg.cooldowns.until.values() if u > now))
    return generate_latest(REGISTRY)
