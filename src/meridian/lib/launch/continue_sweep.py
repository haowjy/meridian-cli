"""Explicit continue's source cleanup, before any new row or harness exists."""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import psutil

from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import birth_time_unverified
from meridian.lib.state.launch_lock import Busy, LaunchHandle, NotFound, acquire
from meridian.lib.state.liveness import is_process_alive_with_birth
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scope_projection
from meridian.lib.state.session_store import chat_record
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, spawn_lock_path


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
                raise ValueError(
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
    return pgid is None or _group_gone(pgid)


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _owner_ids(runtime_root: Path, spawn_id: str) -> tuple[str, ...]:
    source = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    if source is None:
        raise NotFound(f"Spawn '{spawn_id}' not found")
    chat = chat_record(runtime_root, source.chat_id)
    current_id = chat.spawn_id.strip() if chat and chat.spawn_id else ""
    if current_id and current_id != spawn_id:
        return current_id, spawn_id
    return (spawn_id,)


def sweep_before_continue(runtime_root: Path, spawn_id: str, *, dry_run: bool = False) -> None:
    """Refuse a live chat owner; release only safely swept scopes, never status."""
    if dry_run:
        return
    owner_ids = _owner_ids(runtime_root, spawn_id)
    with ExitStack() as stack:
        handles: list[LaunchHandle] = []
        for owner_id in owner_ids:
            try:
                handles.append(stack.enter_context(_acquire_source(runtime_root, owner_id)))
            except Busy as exc:
                if owner_id != spawn_id:
                    raise ValueError(
                        f"{spawn_id} was continued as {owner_id}, which is still running; "
                        f"use spawn inject {owner_id}, --fork {owner_id}, or "
                        f"spawn cancel {owner_id}"
                    ) from exc
                raise ValueError(
                    f"{spawn_id} is still running; "
                    f"use spawn inject {spawn_id} or --fork {spawn_id}"
                ) from exc
        for handle in handles:
            sweep_scopes(runtime_root, handle)


def sweep_scopes(runtime_root: Path, handle: LaunchHandle) -> None:
    """Containment requires an exclusive launch capability, not guessed liveness."""
    spawn_id = handle.spawn_id
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
            if result.skip_reason == "pid_reuse_detected":
                # A PID cannot be reused while it is still a process-group ID.
                # Other groups need their own absence proof; never signal them.
                if scope.pgid not in (None, scope.root_pid) and not _group_gone(scope.pgid):
                    raise ValueError(
                        f"Cannot safely sweep live group {scope.pgid}; use --fork {spawn_id}"
                    )
            elif result.skip_reason == "termination_exception":
                raise ValueError(
                    f"Cannot safely sweep PID {scope.root_pid}: {result.skip_reason}; "
                    f"use --fork {spawn_id}"
                )
        mark_scope_released(runtime_root, spawn_id, scope.release_id)
