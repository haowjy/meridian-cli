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


class PushDelayConfig(Protocol):
    """Config fields needed to choose the push delay for a turn."""

    @property
    def push_seconds(self) -> int: ...

    @property
    def long_turn_seconds(self) -> int: ...

    @property
    def quick_turn_push_seconds(self) -> int: ...

    @property
    def background_push_seconds(self) -> int: ...


@dataclass(frozen=True)
class InFlight:
    """Work still running when a main-loop turn ended, as the adapter and spawn store saw it."""

    # Harness-native subagents and workflows: each wakes the session when it ends.
    agents: int = 0
    # Background shells running `meridian spawn`: each wakes the session when it ends.
    spawn_shells: int = 0
    # The `meridian spawn wait` shells among them, which yield before the cache TTL.
    spawn_waiters: int = 0
    # Any other background task (a shell, an MCP task): it may wake the session, or
    # run for good (a dev server).
    other_tasks: int = 0
    # This session has active child spawns in the spawn store.
    child_spawns: bool = False


def response_hold(work: InFlight) -> str | None:
    """Why the response push waits for a later turn, or None when this turn may push."""

    if work.agents > 0:
        return f"{work.agents} agent(s) running"
    if work.spawn_shells > 0 or work.child_spawns:
        return "meridian spawns running"
    return None


@dataclass(frozen=True)
class Schedule:
    """Absolute epoch-millisecond deadlines for one idle anchor."""

    push_at: int | None
    warn_at: int | None = None
    compact_at: int | None = None


def select_push_delay(
    now_ms: int,
    returned_at_ms: int | None,
    previous_idle_since_ms: int | None,
    cfg: PushDelayConfig,
    work: InFlight | None = None,
) -> int | None:
    """Choose the push delay from the preceding turn length and the work still running.

    None means no push this turn: held work wakes the session when it ends, and
    that turn arms again. Other background tasks only stretch the delay, since a
    dev server may never end.
    """

    work = work or InFlight()
    if response_hold(work) is not None:
        return None
    delay = _turn_push_delay(now_ms, returned_at_ms, previous_idle_since_ms, cfg)
    if work.other_tasks > 0:
        return max(delay, cfg.background_push_seconds)
    return delay


def _turn_push_delay(
    now_ms: int,
    returned_at_ms: int | None,
    previous_idle_since_ms: int | None,
    cfg: PushDelayConfig,
) -> int:
    if cfg.long_turn_seconds == 0:
        return cfg.push_seconds
    if returned_at_ms is None:
        return cfg.quick_turn_push_seconds
    if previous_idle_since_ms is not None and returned_at_ms <= previous_idle_since_ms:
        return cfg.quick_turn_push_seconds
    if now_ms - returned_at_ms >= cfg.long_turn_seconds * 1000:
        return cfg.push_seconds
    return cfg.quick_turn_push_seconds


def schedule(
    idle_since_ms: int,
    ttl_seconds: int | None,
    cfg: TimelineConfig,
    *,
    push_delay_seconds: int | None,
    warn: bool = True,
) -> Schedule:
    """Place stages that fit strictly after their predecessor.

    The push and the cache warning are independent: a held push (None) or a
    suppressed warning leaves the other in place.
    """

    push_at = None if push_delay_seconds is None else idle_since_ms + push_delay_seconds * 1000
    if ttl_seconds is None:
        return Schedule(push_at=push_at)

    expires_at = idle_since_ms + ttl_seconds * 1000
    warn_candidate = expires_at - cfg.warn_minutes * 60 * 1000
    warn_at = warn_candidate if warn and warn_candidate > idle_since_ms else None
    if warn_at is not None and push_at is not None and push_at >= warn_at:
        push_at = None

    compact_candidate = expires_at - cfg.compact_minutes * 60 * 1000
    predecessor = next(
        (at for at in (warn_at, push_at) if at is not None),
        idle_since_ms,
    )
    compact_at = compact_candidate if compact_candidate > predecessor else None
    return Schedule(push_at=push_at, warn_at=warn_at, compact_at=compact_at)


__all__ = [
    "InFlight",
    "PushDelayConfig",
    "Schedule",
    "TimelineConfig",
    "response_hold",
    "schedule",
    "select_push_delay",
]
