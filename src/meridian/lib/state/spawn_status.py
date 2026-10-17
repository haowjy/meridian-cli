"""Read-only spawn status derived from launch-lock liveness."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TerminalSpawnStatus
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state.launch_lock import (
    Busy,
    Contended,
    LaunchHandle,
    NotFound,
    acquire,
    shared_hold,
)
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
    SpawnStateQuarantineReport,
    read_quarantined_chat_id,
    read_state,
)
from meridian.lib.state.timestamps import iso_timestamp_to_epoch

CRASHED = "crashed"


def legacy_runner_alive(row: SpawnRecord) -> bool:
    """Old runners have no flock; preserve their pre-worker startup grace."""
    if row.runner_pid is not None and is_process_alive_with_birth(
        row.runner_pid, row.runner_created_at_epoch
    ):
        return True
    started_at = iso_timestamp_to_epoch(row.started_at)
    return bool(
        not row.runner_pid
        and started_at is not None
        and time.time() - started_at < 15.0
    )


def _derived_terminal(row: SpawnRecord, *, cancelled: bool) -> TerminalFacts:
    """Fit a pure derived outcome into v3's required terminal-facts shape."""
    intent = row.cancel_intent if cancelled else None
    observed_at = (
        row.runner_exit.exited_at
        if row.runner_exit is not None
        else row.last_attempt_exited_at or ""
    )
    return TerminalFacts(
        exit_code=intent.exit_code if intent is not None else 1,
        finished_at=observed_at,
        published_at=observed_at,
        error=intent.error if intent is not None else CRASHED,
        origin="cancel" if intent is not None else "reconciler",
    )


def derive(row: SpawnRecord, lock_free: bool) -> SpawnRecord:
    """Overlay status from one row and its simultaneous lock observation."""
    if not is_active_spawn_status(row.status):
        return row
    if not lock_free:
        return row
    cancelled = row.cancel_intent is not None
    return row.model_copy(
        update={
            "status": "cancelled" if cancelled else "failed",
            "terminal": _derived_terminal(row, cancelled=cancelled),
        }
    )


@dataclass(frozen=True)
class Claimed:
    """Exclusive capability plus the authoritative stored and derived rows."""

    handle: LaunchHandle
    stored: SpawnRecord
    derived: SpawnRecord

    @property
    def status(self) -> TerminalSpawnStatus:
        """Return the terminal status guaranteed by a successful claim."""

        return cast("TerminalSpawnStatus", self.derived.status)

    @property
    def terminal(self) -> TerminalFacts:
        """Return the terminal facts guaranteed by a successful claim."""

        return cast("TerminalFacts", self.derived.terminal)


@dataclass(frozen=True)
class Live:
    """The launch could not be claimed as dead."""


@dataclass(frozen=True)
class Lockless:
    """The row predates runner-lifetime launch locks."""


type DeadLaunchClaim = Claimed | Live | Lockless


def claim_dead_launch(runtime_root: Path, spawn_id: str) -> DeadLaunchClaim:
    """Claim a lock-backed dead launch and return its lock-consistent rows."""
    spawn_dir = runtime_root / "spawns" / spawn_id
    try:
        handle = acquire(spawn_dir)
    except NotFound:
        return Lockless()
    except (Busy, Contended):
        return Live()
    try:
        row = read_state(spawn_dir.parent, spawn_id, include_prompt=False)
        if row is None:
            handle.close()
            return Live()
        derived = derive(row, lock_free=True)
        return Claimed(handle=handle, stored=row, derived=derived)
    except SpawnStateQuarantined:
        handle.close()
        return Live()
    except BaseException:
        handle.close()
        raise


def read_derived(runtime_root: Path, spawn_id: str) -> SpawnRecord | None:
    """Read one row with liveness consistent with its runner-lifetime lock."""
    spawns_dir = runtime_root / "spawns"
    lock_path = spawns_dir / spawn_id / "launch.lock"
    with shared_hold(lock_path) as lock_free:
        row = read_state(spawns_dir, spawn_id, include_prompt=False)
        if row is None:
            return None
        if lock_free is True:
            return derive(row, lock_free=True)
        if lock_free is False:
            return row
    if not is_active_spawn_status(row.status):
        return row
    if legacy_runner_alive(row):
        return row
    return derive(row, lock_free=True)


def overlay_derived(runtime_root: Path, rows: Iterable[SpawnRecord]) -> list[SpawnRecord]:
    """Overlay liveness for stored-active rows from a scan or index query."""
    overlaid: list[SpawnRecord] = []
    for row in rows:
        if not is_active_spawn_status(row.status):
            overlaid.append(row)
            continue
        current = read_derived(runtime_root, row.id)
        overlaid.append(row if current is None else current)
    return overlaid


def is_live(runtime_root: Path, spawn_id: str) -> bool:
    """Return whether the lock-consistent status is active."""

    row = read_derived(runtime_root, spawn_id)
    return row is not None and is_active_spawn_status(row.status)


def quarantine_may_observe(
    runtime_root: Path,
    report: SpawnStateQuarantineReport,
    chat_id: str,
) -> bool:
    """Conservatively decide whether a quarantined row may observe one chat."""

    lock_path = runtime_root / "spawns" / report.spawn_id / "launch.lock"
    with shared_hold(lock_path) as lock_free:
        current_chat_id = read_quarantined_chat_id(report)
        if current_chat_id is None:
            return True
        if current_chat_id != chat_id:
            return False
        return lock_free is not True


__all__ = [
    "CRASHED",
    "Claimed",
    "DeadLaunchClaim",
    "Live",
    "Lockless",
    "claim_dead_launch",
    "derive",
    "is_live",
    "legacy_runner_alive",
    "overlay_derived",
    "quarantine_may_observe",
    "read_derived",
]
