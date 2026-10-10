"""Pure idle-stage timeline calculation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TimelineConfig(Protocol):
    """Config fields needed to place idle stages."""

    @property
    def warn_minutes(self) -> int: ...

    @property
    def compact_minutes(self) -> int: ...


@dataclass(frozen=True)
class Schedule:
    """Absolute epoch-millisecond deadlines for one idle anchor."""

    push_at: int | None
    warn_at: int | None = None
    compact_at: int | None = None


def schedule(
    idle_since_ms: int,
    ttl_seconds: int | None,
    cfg: TimelineConfig,
    *,
    push_delay_seconds: int,
) -> Schedule:
    """Place stages that fit strictly after their predecessor."""

    push_at = idle_since_ms + push_delay_seconds * 1000
    if ttl_seconds is None:
        return Schedule(push_at=push_at)

    expires_at = idle_since_ms + ttl_seconds * 1000
    warn_candidate = expires_at - cfg.warn_minutes * 60 * 1000
    warn_at = warn_candidate if warn_candidate > idle_since_ms else None
    if warn_at is not None and push_at >= warn_at:
        push_at = None

    compact_candidate = expires_at - cfg.compact_minutes * 60 * 1000
    predecessor = warn_at if warn_at is not None else push_at
    assert predecessor is not None
    compact_at = compact_candidate if compact_candidate > predecessor else None
    return Schedule(push_at=push_at, warn_at=warn_at, compact_at=compact_at)


__all__ = ["Schedule", "TimelineConfig", "schedule"]
