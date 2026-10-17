"""Read-only spawn status derived from launch-lock liveness."""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status,
    is_terminal_spawn_status,
)
from meridian.lib.state.launch_lock import shared_hold
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import read_state

CRASHED = "crashed"


def legacy_runner_alive(row: SpawnRecord) -> bool:
    """Old runners have no flock; preserve their pre-worker startup grace."""
    if row.runner_pid is not None and is_process_alive_with_birth(
        row.runner_pid, row.runner_created_at_epoch
    ):
        return True
    return (
        not is_terminal_spawn_status(row.status)
        and not row.runner_pid
        and row.started_at is not None
        and time.time() - datetime.fromisoformat(row.started_at).timestamp() < 15.0
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
    if is_terminal_spawn_status(row.status) or not is_active_spawn_status(row.status):
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
    if is_terminal_spawn_status(row.status) or not is_active_spawn_status(row.status):
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


__all__ = ["CRASHED", "derive", "legacy_runner_alive", "overlay_derived", "read_derived"]
