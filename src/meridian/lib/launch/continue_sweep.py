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
from meridian.lib.state.launch_lock import (
    Busy,
    LaunchHandle,
    NotFound,
    acquire,
    lock_path,
    shared_try,
)
from meridian.lib.state.liveness import is_process_dead_with_birth
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scope_projection
from meridian.lib.state.session_store import chat_record
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import SpawnStateQuarantined, read_state, spawn_lock_path
from meridian.lib.state.spawn_status import is_live, legacy_runner_alive


def _legacy_live_error(spawn_id: str) -> ValueError:
    return ValueError(f"{spawn_id} is running under an older meridian; wait, or --fork {spawn_id}")


def _acquire_source(runtime_root: Path, spawn_id: str) -> LaunchHandle:
    spawns_dir = runtime_root / "spawns"
    path = lock_path(runtime_root, spawn_id)
    if path.exists():
        return acquire(runtime_root, spawn_id)
    # Old runners use this same mutation lock. Re-read before converting;
    # another continue may have created the lifetime lock while we waited.
    with lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False):
        row = read_state(spawns_dir, spawn_id, include_prompt=False)
        if row is None:
            raise NotFound(f"Spawn '{spawn_id}' not found")
        if not path.exists():
            if not is_terminal_spawn_status(row.status) and legacy_runner_alive(row):
                raise _legacy_live_error(spawn_id)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            os.close(fd)
        return acquire(runtime_root, spawn_id)


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


def _owners(runtime_root: Path, spawn_id: str, chat_id: str | None) -> tuple[SpawnRecord, ...]:
    source = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    chat = chat_record(runtime_root, chat_id) if chat_id else None
    current_id = chat.spawn_id.strip() if chat and chat.spawn_id else ""
    owners: list[SpawnRecord] = []
    if current_id and current_id != spawn_id:
        current = read_state(runtime_root / "spawns", current_id, include_prompt=False)
        if current is not None:
            owners.append(current)
    if source is not None:
        owners.append(source)
    return tuple(owners)


def _recorded_runner_dead(runtime_root: Path, spawn_id: str) -> bool:
    """Whether a lock holder can't be the recorded runner (it is provably gone)."""
    try:
        row = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    except SpawnStateQuarantined:
        return False
    return row is not None and is_process_dead_with_birth(
        row.runner_pid, row.runner_created_at_epoch
    )


def _live_owner_error(
    runtime_root: Path, spawn_id: str, owner_id: str, chat_id: str | None
) -> ValueError:
    if owner_id != spawn_id:
        return ValueError(
            f"{spawn_id} was continued as {owner_id}, which is still running; "
            f"use spawn message {owner_id}, --fork {owner_id}, or spawn cancel {owner_id}"
        )
    row = read_state(runtime_root / "spawns", owner_id, include_prompt=False)
    moved_to = row.continue_chat_id or row.current_chat_id if row is not None else None
    if chat_id and moved_to and moved_to != chat_id:
        # The owner keeps its entry chat's lifetime lock until it exits.
        return ValueError(
            f"{spawn_id} moved to {moved_to} but still holds {chat_id} until it exits; "
            f"continue after it exits, or --fork {chat_id}"
        )
    return ValueError(
        f"{spawn_id} is still running; use spawn message {spawn_id} or --fork {spawn_id}"
    )


def _busy_owner_error(
    runtime_root: Path, spawn_id: str, owner_id: str, chat_id: str | None
) -> ValueError:
    if _recorded_runner_dead(runtime_root, owner_id):
        return ValueError(
            f"{owner_id}'s launch lock is held by a repair or another continue; "
            "retry in a moment"
        )
    return _live_owner_error(runtime_root, spawn_id, owner_id, chat_id)


def sweep_before_continue(
    runtime_root: Path,
    spawn_id: str,
    *,
    chat_id: str | None,
    dry_run: bool = False,
) -> None:
    """Refuse a live chat owner; release only safely swept scopes, never status."""
    owners = _owners(runtime_root, spawn_id, chat_id)
    if dry_run:
        for owner in owners:
            owner_id = owner.id
            lock_free = shared_try(lock_path(runtime_root, owner_id))
            if lock_free is False:
                raise _busy_owner_error(runtime_root, spawn_id, owner_id, chat_id)
            if lock_free is None and is_live(runtime_root, owner_id):
                raise _legacy_live_error(owner_id)
        return
    with ExitStack() as stack:
        held: list[tuple[SpawnRecord, LaunchHandle]] = []
        for owner in owners:
            try:
                held.append((owner, stack.enter_context(_acquire_source(runtime_root, owner.id))))
            except NotFound:
                continue
            except Busy as exc:
                raise _busy_owner_error(runtime_root, spawn_id, owner.id, chat_id) from exc
        for owner, handle in held:
            sweep_scopes(LaunchPaths.for_row(runtime_root, owner), handle)


def sweep_scopes(launch: LaunchPaths, handle: LaunchHandle) -> None:
    """Containment requires an exclusive launch capability, not guessed liveness."""
    spawn_id = handle.spawn_id
    projection = read_scope_projection(launch)
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
        mark_scope_released(launch, scope.release_id)
