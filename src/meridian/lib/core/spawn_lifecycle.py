"""The spawn status vocabulary and cancellation timing every layer shares."""

from __future__ import annotations

from typing import cast

from typing_extensions import TypeIs

from meridian.lib.core.domain import (
    ACTIVE_SPAWN_STATUSES,
    ALL_SPAWN_STATUSES,
    TERMINAL_SPAWN_STATUSES,
    SpawnStatus,
    TerminalSpawnStatus,
)

# Leave time for the terminal write before cancellation escalates to SIGKILL,
# which would lose the runner's usage.
CANCEL_GRACE_SECS = 5.0
SIGNAL_STOP_BUDGET_SECS = CANCEL_GRACE_SECS - 2.0

FAILURE_SPAWN_STATUSES: frozenset[str] = frozenset({"failed", "timed_out"})


def is_active_spawn_status(status: str) -> bool:
    return status in ACTIVE_SPAWN_STATUSES


def is_terminal_spawn_status(status: str) -> TypeIs[TerminalSpawnStatus]:
    return status in TERMINAL_SPAWN_STATUSES


def parse_terminal_status(status: str) -> TerminalSpawnStatus:
    if is_terminal_spawn_status(status):
        return status
    raise ValueError(f"Invalid terminal spawn status: {status!r}")


def coerce_spawn_status(status: str) -> SpawnStatus:

    if status in ALL_SPAWN_STATUSES:
        return cast("SpawnStatus", status)
    return SpawnStatus.FAILED
