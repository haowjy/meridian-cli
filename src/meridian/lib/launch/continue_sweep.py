"""Explicit continue's source cleanup, before any new row or harness exists."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path

import psutil

from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import birth_time_unverified
from meridian.lib.state.launch_lock import Busy, LaunchHandle, NotFound, acquire
from meridian.lib.state.liveness import is_process_dead_with_birth
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scope_projection
from meridian.lib.state.session_store import chat_record
from meridian.lib.state.spawn.repository import SpawnStateQuarantined, read_state, spawn_lock_path
from meridian.lib.state.spawn_status import is_live, legacy_runner_alive


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
            if not is_terminal_spawn_status(row.status) and legacy_runner_alive(row):
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


def _owner_ids(runtime_root: Path, spawn_id: str, chat_id: str | None) -> tuple[str, ...]:
    source = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    chat = chat_record(runtime_root, chat_id) if chat_id else None
    current_id = chat.spawn_id.strip() if chat and chat.spawn_id else ""
    owner_ids: list[str] = []
    if (
        current_id
        and current_id != spawn_id
        and read_state(runtime_root / "spawns", current_id, include_prompt=False) is not None
    ):
        owner_ids.append(current_id)
    if source is not None:
        owner_ids.append(spawn_id)
    return tuple(owner_ids)


def _recorded_runner_dead(runtime_root: Path, spawn_id: str) -> bool:
    """Whether a lock holder can't be the recorded runner (it is provably gone)."""
    try:
        row = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    except SpawnStateQuarantined:
        return False
    return row is not None and is_process_dead_with_birth(
        row.runner_pid, row.runner_created_at_epoch
    )


def _live_owner_error(spawn_id: str, owner_id: str) -> ValueError:
    if owner_id != spawn_id:
        return ValueError(
            f"{spawn_id} was continued as {owner_id}, which is still running; "
            f"use spawn inject {owner_id}, --fork {owner_id}, or spawn cancel {owner_id}"
        )
    return ValueError(
        f"{spawn_id} is still running; use spawn inject {spawn_id} or --fork {spawn_id}"
    )


def sweep_before_continue(
    runtime_root: Path,
    spawn_id: str,
    *,
    chat_id: str | None,
    dry_run: bool = False,
) -> None:
    """Refuse a live chat owner; release only safely swept scopes, never status."""
    owner_ids = _owner_ids(runtime_root, spawn_id, chat_id)
    if dry_run:
        for owner_id in owner_ids:
            if is_live(runtime_root, owner_id):
                raise _live_owner_error(spawn_id, owner_id)
        return
    with ExitStack() as stack:
        handles: list[LaunchHandle] = []
        for owner_id in owner_ids:
            try:
                handles.append(stack.enter_context(_acquire_source(runtime_root, owner_id)))
            except Busy as exc:
                if _recorded_runner_dead(runtime_root, owner_id):
                    raise ValueError(
                        f"{owner_id}'s launch lock is held by a repair or another continue; "
                        "retry in a moment"
                    ) from exc
                raise _live_owner_error(spawn_id, owner_id) from exc
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
