"""Crash-only signal files for resident spawn coordination."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args

from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

SpawnSignalKind = Literal["done", "rearm"]
_SIGNAL_KINDS: frozenset[str] = frozenset(get_args(SpawnSignalKind))


@dataclass(frozen=True)
class RearmRequest:
    """What a `rearm` asked for. A file an older build wrote carries neither."""

    long: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class RearmRecord:
    """One granted rearm, as `spawn show` reads it."""

    at: str
    long: bool
    reason: str | None


@dataclass(frozen=True)
class HoldStatus:
    """What a held child waits on. Advisory: a stale or missing file only hides a line."""

    blockers: tuple[str, ...]
    last_work_at: str | None


@dataclass(frozen=True)
class ResidentSignals:
    """Resident coordination signals consumed in one crash-tolerant read."""

    done: bool = False
    rearm: bool = False
    rearm_request: RearmRequest | None = None


def spawn_signal_path(launch: LaunchPaths, kind: SpawnSignalKind) -> Path:
    """Return the authoritative path for a resident spawn signal file."""

    return launch.done_signal if kind == "done" else launch.rearm_signal


def write_accepted_signals(
    launch: LaunchPaths,
    kinds: Iterable[SpawnSignalKind],
) -> None:
    """Declare the signals this spawn's drain plan consumes.

    A spawn-dir file rather than a row field: spawn rows forbid unknown keys, so
    older builds would quarantine every row carrying one.
    """

    payload = json.dumps(sorted(kinds))
    mutate_published_spawn_artifact(
        launch.runtime_root,
        launch.spawn_id,
        lambda: atomic_write_text(launch.accepted_signals, payload),
    )


def read_accepted_signals(launch: LaunchPaths) -> frozenset[SpawnSignalKind] | None:
    """Return the declared signals, or ``None`` when the runner hasn't declared any.

    ``None`` covers a plan not chosen yet, a runner that predates the file, and an
    unreadable file.
    """

    try:
        raw: object = json.loads(launch.accepted_signals.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    kinds = cast("list[object]", raw)
    if not all(isinstance(kind, str) and kind in _SIGNAL_KINDS for kind in kinds):
        return None
    return cast("frozenset[SpawnSignalKind]", frozenset(kinds))


def write_spawn_signal(
    launch: LaunchPaths,
    kind: SpawnSignalKind,
    *,
    can_write: Callable[[SpawnRecord], bool] | None = None,
    rearm: RearmRequest | None = None,
) -> bool:
    """Write an idempotent signal while its published spawn still exists.

    ``can_write`` sees the published row under the same lock as the write. ``rearm`` is the
    payload; the file's presence stays the signal, so an older runner still reads it.
    """

    body = _timestamp()
    if rearm is not None:
        body = json.dumps({"at": body, "long": rearm.long, "reason": rearm.reason})
    return mutate_published_spawn_artifact(
        launch.runtime_root,
        launch.spawn_id,
        lambda: atomic_write_text(spawn_signal_path(launch, kind), f"{body}\n"),
        can_mutate=can_write,
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_object(text: str) -> dict[str, object] | None:
    try:
        raw: object = json.loads(text)
    except ValueError:
        return None
    return cast("dict[str, object]", raw) if isinstance(raw, dict) else None


def _read_rearm_request(launch: LaunchPaths) -> RearmRequest:
    """Read a rearm's payload. Older builds wrote a bare timestamp, which parses to no object."""

    try:
        payload = _json_object(launch.rearm_signal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if payload is None:
        return RearmRequest()
    reason = payload.get("reason")
    return RearmRequest(
        long=payload.get("long") is True, reason=reason if isinstance(reason, str) else None
    )


def consume_spawn_signal(launch: LaunchPaths, kind: SpawnSignalKind) -> bool:
    """Delete a signal file if present, tolerating races and crash leftovers."""

    path = spawn_signal_path(launch, kind)
    if not path.exists():
        return False
    with suppress(FileNotFoundError):
        path.unlink()
        return True
    return False


def _written_at(launch: LaunchPaths, kind: SpawnSignalKind) -> int:
    with suppress(OSError):
        return spawn_signal_path(launch, kind).stat().st_mtime_ns
    return 0


def consume_resident_signals(launch: LaunchPaths) -> ResidentSignals:
    """Consume resident done/rearm signal files in a single coordination call.

    Of two read together the later stands: a first turn's signals wait for its end, so a
    `rearm` then a `done` can share a read. A tie stays a rearm.
    """

    done_at = _written_at(launch, "done")
    rearm_at = _written_at(launch, "rearm")
    done = consume_spawn_signal(launch, "done")
    request = _read_rearm_request(launch) if launch.rearm_signal.exists() else None
    rearm = consume_spawn_signal(launch, "rearm")
    if done and rearm:
        done, rearm = done_at > rearm_at, rearm_at >= done_at
    return ResidentSignals(done=done, rearm=rearm, rearm_request=request if rearm else None)


def record_rearm(launch: LaunchPaths, request: RearmRequest) -> None:
    """Append one granted rearm; a spawn outlives its signal file, and a reason should too."""

    line = json.dumps({"at": _timestamp(), "long": request.long, "reason": request.reason})

    def append() -> None:
        with launch.rearm_log.open("a", encoding="utf-8") as log:
            log.write(f"{line}\n")

    mutate_published_spawn_artifact(launch.runtime_root, launch.spawn_id, append)


def read_rearms(launch: LaunchPaths) -> tuple[RearmRecord, ...]:
    """Read the granted rearms, skipping any line a crash cut short."""

    try:
        lines = launch.rearm_log.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return ()
    records: list[RearmRecord] = []
    for line in lines:
        entry = _json_object(line)
        if entry is None:
            continue
        at = entry.get("at")
        reason = entry.get("reason")
        if isinstance(at, str):
            records.append(
                RearmRecord(
                    at=at,
                    long=entry.get("long") is True,
                    reason=reason if isinstance(reason, str) else None,
                )
            )
    return tuple(records)


def write_hold_status(launch: LaunchPaths, status: HoldStatus | None) -> None:
    """Replace ``hold.json``, or remove it when the child is no longer held."""

    def replace_status() -> None:
        if status is None:
            launch.hold_status.unlink(missing_ok=True)
            return
        payload = {"blockers": list(status.blockers), "last_work_at": status.last_work_at}
        atomic_write_text(launch.hold_status, json.dumps(payload))

    mutate_published_spawn_artifact(launch.runtime_root, launch.spawn_id, replace_status)


def read_hold_status(launch: LaunchPaths) -> HoldStatus | None:
    """Return what a held child waits on, or ``None`` when that can't be read."""

    try:
        payload = _json_object(launch.hold_status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload is None:
        return None
    blockers = payload.get("blockers")
    last_work_at = payload.get("last_work_at")
    if not isinstance(blockers, list):
        return None
    labels = cast("list[object]", blockers)
    return HoldStatus(
        blockers=tuple(label for label in labels if isinstance(label, str)),
        last_work_at=last_work_at if isinstance(last_work_at, str) else None,
    )
