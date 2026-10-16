"""Explicit continue's source cleanup, before any new row or harness exists."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import psutil

from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import birth_time_unverified
from meridian.lib.state.launch_lock import Busy, LaunchHandle, NotFound, acquire
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scope_projection
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path


def legacy_runner_alive(row: SpawnRecord) -> bool:
    """Old runners have no flock; preserve their pre-worker startup grace."""
    if any(
        pid is not None and is_process_alive_with_birth(pid, row.runner_created_at_epoch)
        for pid in (row.runner_pid, row.worker_pid)
    ):
        return True
    return (
        not is_terminal_spawn_status(row.status)
        and not row.runner_pid
        and row.started_at is not None
        and time.time() - datetime.fromisoformat(row.started_at).timestamp() < 15.0
    )


def _acquire_source(runtime_root: Path, spawn_id: str) -> LaunchHandle:
    spawns_dir = runtime_root / "spawns"
    spawn_dir = spawns_dir / spawn_id
    if (spawn_dir / "launch.lock").exists():
        return acquire(spawn_dir)
    # Old runners use this same mutation lock. Re-read before converting;
    # another continue may have created the lifetime lock while we waited.
    with lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False):
        row = read_state(spawns_dir, spawn_id, include_prompt=False)
        if row is None:
            raise NotFound(f"Spawn '{spawn_id}' not found")
        if not (spawn_dir / "launch.lock").exists():
            if legacy_runner_alive(row):
                raise Busy(
                    f"{spawn_id} is running under an older meridian; wait, or --fork {spawn_id}"
                )
            fd = os.open(
                spawn_dir / "launch.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
            )
            os.close(fd)
        return acquire(spawn_dir)


def _unknown_scope_gone(pid: int, pgid: int | None) -> bool:
    # Signal 0 only. Permission errors and zombies are not proof of absence.
    if psutil.pid_exists(pid):
        return False
    if pgid is not None:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False
    return True


def sweep_before_continue(runtime_root: Path, spawn_id: str) -> None:
    """Refuse a live source; release only safely swept scopes, never status."""
    with _acquire_source(runtime_root, spawn_id) as handle:
        sweep_scopes(runtime_root, handle)


def sweep_scopes(runtime_root: Path, handle: LaunchHandle) -> None:
    """Containment requires an exclusive launch capability, not guessed liveness."""
    if handle.spawn_id is None or handle.fd < 0:
        raise ValueError("Sweep requires a held launch lock")
    spawn_id = SpawnId(handle.spawn_id)
    projection = read_scope_projection(runtime_root, spawn_id)
    for scope in projection.scopes:
        if scope.release_id in projection.released_ids:
            continue
        if birth_time_unverified(scope.root_created_at_epoch):
            if not _unknown_scope_gone(scope.root_pid, scope.pgid):
                raise ValueError(
                    f"Cannot safely sweep PID {scope.root_pid} with unknown birth; "
                    f"use --fork {spawn_id} to branch without touching it"
                )
        else:
            result = terminate_scope_sync(scope, grace_seconds=5.0, reason="continue_sweep")
            if result.skip_reason is not None:
                raise ValueError(
                    f"Cannot safely sweep PID {scope.root_pid}: {result.skip_reason}; "
                    f"use --fork {spawn_id}"
                )
        mark_scope_released(runtime_root, spawn_id, scope.release_id)
