"""Pure idle-stage timeline calculation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from meridian.lib.idle.wake import NO_WAKE, ExpectedWake


class TimelineConfig(Protocol):
    """Config fields needed to place idle stages."""

    @property
    def push_seconds(self) -> int: ...

    @property
    def long_turn_seconds(self) -> int: ...

    @property
    def quick_turn_push_seconds(self) -> int: ...

    @property
    def warn_minutes(self) -> int: ...


@dataclass(frozen=True)
class Schedule:
    """Absolute epoch-millisecond deadlines for one idle anchor."""

    push_at: int | None
    warn_at: int | None = None


def push_delay(now_ms: int, owed_since_ms: int | None, cfg: TimelineConfig) -> int:
    """Push soon after a long wait for this response; wait longer after a quick turn.

    `owed_since_ms` is when the user started waiting for a response no push has
    answered yet. A turn that follows an earlier push (a background wake) is
    treated as quick: the user may be reading.
    """

    if cfg.long_turn_seconds == 0:
        return cfg.push_seconds
    if owed_since_ms is not None and now_ms - owed_since_ms >= cfg.long_turn_seconds * 1000:
        return cfg.push_seconds
    return cfg.quick_turn_push_seconds


def schedule(
    idle_since_ms: int,
    ttl_seconds: int | None,
    cfg: TimelineConfig,
    *,
    push_delay_seconds: int,
    wake: ExpectedWake = NO_WAKE,
) -> Schedule:
    """Place the push and the cache warning, each from what the running work says.

    No push when a wake is certain: that turn arms again. Work that may never
    end only delays it. The warning is dropped only when a known wake lands
    before the cache goes cold, since that wake keeps it warm.
    """

    push_at: int | None = None
    if wake.certain is None:
        delay = push_delay_seconds
        if wake.maybe:
            delay = max(delay, cfg.quick_turn_push_seconds)
        push_at = idle_since_ms + delay * 1000
    if ttl_seconds is None:
        return Schedule(push_at=push_at)

    expires_at = idle_since_ms + ttl_seconds * 1000
    kept_warm = wake.by_ms is not None and wake.by_ms < expires_at
    warn_candidate = expires_at - cfg.warn_minutes * 60 * 1000
    warn_at = warn_candidate if not kept_warm and warn_candidate > idle_since_ms else None
    if warn_at is not None and push_at is not None and push_at >= warn_at:
        push_at = None
    return Schedule(push_at=push_at, warn_at=warn_at)


__all__ = ["Schedule", "TimelineConfig", "push_delay", "schedule"]
