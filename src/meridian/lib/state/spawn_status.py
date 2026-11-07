from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state.json_doc import read_model
from meridian.lib.state.launch_lock import (
    Busy,
    Contended,
    LaunchHandle,
    NotFound,
    acquire,
    lock_path,
    shared_hold,
)
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import CancelIntent, SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import (
    QuarantinedChatIdStatus,
    SpawnStateQuarantined,
    SpawnStateQuarantineReport,
    read_launch_archive,
    read_quarantined_chat_id,
    read_state,
)
from meridian.lib.state.spawn_report import spawn_report_has_durable_completion
from meridian.lib.state.timestamps import epoch_to_iso_timestamp, iso_timestamp_to_epoch

CRASHED = "crashed"


def legacy_runner_alive(row: SpawnRecord) -> bool:
    """Old runners have no flock; preserve their pre-worker startup grace."""
    if row.runner_pid is not None and is_process_alive_with_birth(
        row.runner_pid, row.runner_created_at_epoch
    ):
        return True
    started_at = iso_timestamp_to_epoch(row.started_at)
    return bool(not row.runner_pid and started_at is not None and time.time() - started_at < 15.0)


def read_cancel_intent(launch: LaunchPaths, row: SpawnRecord) -> CancelIntent | None:
    """The launch's cancel request: ``cancel.json``, else the copy an older cancel left."""
    return read_model(launch.cancel, CancelIntent) or row.cancel_intent


def _observed_end(launch: LaunchPaths, row: SpawnRecord) -> str:
    if row.runner_exit is not None:
        return row.runner_exit.exited_at
    if row.last_attempt_exited_at:
        return row.last_attempt_exited_at
    mtimes: list[float] = []
    for path in launch.runner_activity:
        with suppress(OSError):
            mtimes.append(path.stat().st_mtime)
    return epoch_to_iso_timestamp(max(mtimes)) if mtimes else row.started_at or ""


def derive(runtime_root: Path, row: SpawnRecord) -> SpawnRecord:
    if not is_active_spawn_status(row.status):
        return row
    launch = LaunchPaths.for_row(runtime_root, row)
    intent = read_cancel_intent(launch, row)
    observed_at = _observed_end(launch, row)
    if spawn_report_has_durable_completion(launch):
        status, exit_code, error, origin = SpawnStatus.SUCCEEDED, 0, None, "reconciler"
    elif intent is None:
        status, exit_code, error, origin = SpawnStatus.FAILED, 1, CRASHED, "reconciler"
    else:
        status, exit_code, error, origin = (
            SpawnStatus.CANCELLED,
            intent.exit_code,
            intent.error,
            "cancel",
        )
    return row.model_copy(
        update={
            "status": status,
            "terminal": TerminalFacts(
                exit_code=exit_code,
                finished_at=observed_at,
                published_at=observed_at,
                error=error,
                origin=origin,
            ),
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

        return cast("TerminalSpawnStatus", self.derived.status)

    @property
    def terminal(self) -> TerminalFacts:

        return cast("TerminalFacts", self.derived.terminal)


@dataclass(frozen=True)
class Settled:
    """Exclusive capability plus an authoritative stored non-active row."""

    handle: LaunchHandle
    stored: SpawnRecord


@dataclass(frozen=True)
class Live:
    """The launch could not be claimed as dead."""


@dataclass(frozen=True)
class Lockless:
    """The row predates runner-lifetime launch locks."""


type DeadLaunchClaim = Claimed | Settled | Live | Lockless


def claim_dead_launch(
    runtime_root: Path, spawn_id: str, *, expected_n: int | None = None
) -> DeadLaunchClaim:
    try:
        handle = acquire(runtime_root, spawn_id)
    except NotFound:
        return Lockless()
    except (Busy, Contended):
        return Live()
    try:
        row = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
        if row is None or (expected_n is not None and row.launch_number != expected_n):
            handle.close()
            return Live()
        if not is_active_spawn_status(row.status):
            return Settled(handle=handle, stored=row)
        derived = derive(runtime_root, row)
        return Claimed(handle=handle, stored=row, derived=derived)
    except SpawnStateQuarantined:
        handle.close()
        return Live()
    except BaseException:
        handle.close()
        raise


def _read_current(runtime_root: Path, spawn_id: str) -> tuple[SpawnRecord | None, bool]:
    spawns_dir = runtime_root / "spawns"
    with shared_hold(lock_path(runtime_root, spawn_id)) as lock_free:
        row = read_state(spawns_dir, spawn_id, include_prompt=False)
        if row is None:
            return None, False
        if lock_free is True:
            return derive(runtime_root, row), is_active_spawn_status(row.status)
        if lock_free is False:
            return row, False
    if not is_active_spawn_status(row.status) or legacy_runner_alive(row):
        return row, False
    return derive(runtime_root, row), True


def _read_launch(runtime_root: Path, spawn_id: str, n: int) -> tuple[SpawnRecord | None, bool]:
    row, derived = _read_current(runtime_root, spawn_id)
    if row is not None and row.launch_number > n:
        return read_launch_archive(runtime_root, spawn_id, n), False
    return row, derived


def read_derived(runtime_root: Path, spawn_id: str) -> SpawnRecord | None:
    return _read_current(runtime_root, spawn_id)[0]


def read_launch(runtime_root: Path, spawn_id: str, n: int) -> SpawnRecord | None:
    return _read_launch(runtime_root, spawn_id, n)[0]


@dataclass(frozen=True)
class Finished:
    """The launch has a stored terminal."""

    row: SpawnRecord


@dataclass(frozen=True)
class LockFree:
    """No runner holds the launch: nothing will write its outcome. ``row`` is the derived one."""

    row: SpawnRecord


@dataclass(frozen=True)
class Running:
    """A runner holds the launch. ``row`` is its current (active) row."""

    row: SpawnRecord


type LaunchPoll = Finished | LockFree | Running

POLL_INTERVAL_SECS = 0.1


class LaunchNotFound(ValueError):
    """The spawn has no row (never existed, or was deleted or archived)."""


def poll_launch(runtime_root: Path, spawn_id: str, n: int) -> LaunchPoll:
    row, derived = _read_launch(runtime_root, spawn_id, n)
    if row is None:
        current = read_derived(runtime_root, spawn_id)
        if current is not None:
            raise LaunchNotFound(
                f"Launch {n} of spawn '{spawn_id}' has no archive "
                f"(the spawn is at launch {current.launch_number})"
            )
        raise LaunchNotFound(f"Spawn '{spawn_id}' not found")
    if derived:
        return LockFree(row)
    return Running(row) if is_active_spawn_status(row.status) else Finished(row)


async def wait_until(runtime_root: Path, spawn_id: str, n: int, *, until: float) -> LaunchPoll:
    while True:
        poll = poll_launch(runtime_root, spawn_id, n)
        remaining = until - time.monotonic()
        if not isinstance(poll, Running) or remaining <= 0:
            return poll
        await asyncio.sleep(min(POLL_INTERVAL_SECS, remaining))


def overlay_derived(runtime_root: Path, rows: Iterable[SpawnRecord]) -> list[SpawnRecord]:
    overlaid: list[SpawnRecord] = []
    for row in rows:
        if not is_active_spawn_status(row.status):
            overlaid.append(row)
            continue
        current = read_derived(runtime_root, row.id)
        overlaid.append(row if current is None else current)
    return overlaid


def is_live(runtime_root: Path, spawn_id: str) -> bool:

    row = read_derived(runtime_root, spawn_id)
    return row is not None and is_active_spawn_status(row.status)


def quarantine_may_observe(
    runtime_root: Path,
    report: SpawnStateQuarantineReport,
    chat_id: str,
) -> bool:
    with shared_hold(lock_path(runtime_root, report.spawn_id)) as lock_free:
        if report.newer_version is not None:
            # A later build's runner holds the lock for its whole life; a free lock is no observer.
            return chat_id in report.newer_chat_ids and lock_free is not True
        current_chat_id = read_quarantined_chat_id(report)
        if current_chat_id is QuarantinedChatIdStatus.UNREADABLE:
            return True
        if isinstance(current_chat_id, str) and current_chat_id != chat_id:
            return False
        return lock_free is not True


__all__ = [
    "CRASHED",
    "POLL_INTERVAL_SECS",
    "Claimed",
    "DeadLaunchClaim",
    "Finished",
    "LaunchNotFound",
    "LaunchPoll",
    "Live",
    "LockFree",
    "Lockless",
    "Running",
    "Settled",
    "claim_dead_launch",
    "derive",
    "is_live",
    "legacy_runner_alive",
    "overlay_derived",
    "poll_launch",
    "quarantine_may_observe",
    "read_cancel_intent",
    "read_derived",
    "read_launch",
    "wait_until",
]
