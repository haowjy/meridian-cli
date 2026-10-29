"""Explicit continue, steps 1-4: take a finished spawn's launch lock and leave it ready to reopen.

A continue keeps the spawn's ID. ``resolve_continue_target`` decides which spawn a continue
reopens; the caller builds the whole launch from that spawn, then ``begin_continue`` takes its
launch lock, ends the last launch (outcome written, processes swept, reaper claim resolved)
and checks what would make the next one fail. Preparing the launch and committing it
(``spawn_store.reopen_spawn``) belong to the caller, which holds the returned ``ContinueHold``
until its runner owns the lock. Nothing is written for ``dry_run``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil

from meridian.lib.core.spawn_lifecycle import is_terminal_spawn_status
from meridian.lib.platform.locking import lock_file, try_lock_file
from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import birth_time_unverified
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import (
    Busy,
    Contended,
    LaunchHandle,
    LaunchSlot,
    NotFound,
    acquire,
    lock_path,
    shared_try,
)
from meridian.lib.state.liveness import is_process_alive_with_birth, is_process_dead_with_birth
from meridian.lib.state.managed_primary import terminate_managed_primary_processes
from meridian.lib.state.paths import LaunchPaths, RuntimePaths
from meridian.lib.state.primary_meta import PrimaryMetadata, read_primary_metadata
from meridian.lib.state.process_scope_projection import mark_scope_released, read_scope_projection
from meridian.lib.state.reaper import resolve_cleanup_claim
from meridian.lib.state.reaper_cleanup_claim import read_cleanup_claim
from meridian.lib.state.session_identity import chat_owner_spawn
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    Applied,
    read_state,
    spawn_lock_path,
)
from meridian.lib.state.spawn_status import derive, is_live, legacy_runner_alive
from meridian.plugin_api.types import SpawnKind

_TEARDOWN_GRACE_SECS = 5.0


@dataclass(frozen=True)
class ContinueHold:
    """The reopened spawn's launch lock, held with its last launch finished and swept.

    ``slot`` owns the lock: the caller enters it once (``with hold.slot``), and what it calls
    uses it without closing; the runner's handover closes it early.
    ``spawn`` is the stored row, whose launch has an outcome.
    """

    slot: LaunchSlot
    spawn: SpawnRecord


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


def _busy_error(
    runtime_root: Path, spawn_id: str, chat_id: str | None, caller_spawn_id: str | None
) -> ValueError:
    row = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
    if row is not None and is_process_dead_with_birth(row.runner_pid, row.runner_created_at_epoch):
        return ValueError(f"{spawn_id} is being continued or repaired")
    if caller_spawn_id == spawn_id:
        return ValueError(f"{spawn_id} is this session; use --fork {spawn_id}")
    moved_to = (row.continue_chat_id or row.current_chat_id) if row is not None else None
    if chat_id and moved_to and moved_to != chat_id:
        # The runner keeps its entry chat's lifetime lock until it exits.
        return ValueError(
            f"{spawn_id} moved to {moved_to} but still holds {chat_id} until it exits; "
            f"continue after it exits, or --fork {chat_id}"
        )
    return ValueError(
        f"{spawn_id} is still running; use spawn message {spawn_id} or --fork {spawn_id}"
    )


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


def _sweep_scopes(launch: LaunchPaths, spawn_id: str) -> None:
    """Stop every scope the last launch left, or refuse: containment needs proof, not a guess."""
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


def resolve_continue_target(
    runtime_root: Path,
    named: SpawnRecord,
    *,
    named_by_spawn_ref: bool,
    chat_id: str | None,
    parent_id: str | None,
    kind: SpawnKind,
) -> tuple[SpawnRecord, str | None]:
    """Step 1: the spawn this continue reopens, and a notice when it is not the one named.

    Pure reads. The caller builds the launch (model, goal, policy, session) from the returned
    spawn, never from ``named``.

    A chat's conversation belongs to the spawn that ran it last, so naming an older spawn of the
    chat (chains continued before IDs were kept) or the chat itself reopens that spawn. The
    caller never moves a spawn it did not name: a different parent or kind refuses.
    """
    owner_id = chat_owner_spawn(runtime_root, chat_id) if chat_id else None
    if owner_id is None or str(owner_id) == named.id:
        target = named
    else:
        owner = spawn_store.get_spawn(runtime_root, str(owner_id))
        target = owner if owner is not None else named
    if target.record_mode == "historical":
        raise ValueError(f"{target.id} is a historical record; use --fork {target.id}")
    redirected = target.id != named.id
    if (redirected or not named_by_spawn_ref) and (
        target.parent_id != parent_id or target.kind != kind
    ):
        if redirected:
            raise ValueError(
                f"{named.id}'s conversation now belongs to {target.id} under a different "
                f"caller; continue {target.id} from there, or --fork {named.id}"
            )
        if target.kind != kind:
            command = "meridian --continue" if kind == "primary" else "meridian spawn --continue"
            raise ValueError(
                f"{chat_id} is {target.kind} {target.id}'s conversation; "
                f"`{command} {target.id}` makes it a {kind}, or --fork {chat_id}"
            )
        raise ValueError(
            f"Chat {chat_id} belongs to {target.id}, which runs under a different caller; "
            f"continue {target.id} by its ID to move it, or --fork {chat_id}"
        )
    notice = f"{named.id}'s conversation continues as {target.id}" if redirected else None
    return target, notice


def _finish_last_launch(
    project_root: Path, runtime_root: Path, handle: LaunchHandle
) -> SpawnRecord:
    """Step 3: with the lock held, no runner of the last launch is alive. End it."""
    from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots

    spawn_id = str(handle.spawn_id)
    spawns_dir = runtime_root / "spawns"
    row = read_state(spawns_dir, spawn_id, include_prompt=False)
    if row is None:
        raise NotFound(f"Spawn '{spawn_id}' not found")
    launch = LaunchPaths.for_row(runtime_root, row)
    if row.terminal is None:
        derived = derive(runtime_root, row)
        terminal = derived.terminal
        assert terminal is not None
        outcome = build_spawn_lifecycle_service_from_roots(project_root, runtime_root).finalize(
            spawn_id,
            cast("spawn_store.TerminalSpawnStatus", derived.status),
            terminal.exit_code,
            origin=terminal.origin,
            error=terminal.error,
            finished_at=terminal.finished_at,
            expected_n=row.launch_number,
        )
        if not isinstance(outcome, Applied):
            raise ValueError(f"{spawn_id} changed while it was being continued; retry")
        row = outcome.after
    # A claim records processes of the last launch whatever ended it; none may reach the next.
    if read_cleanup_claim(runtime_root, spawn_id):
        resolve_cleanup_claim(runtime_root, row)
        if read_cleanup_claim(runtime_root, spawn_id):
            raise ValueError(
                f"{spawn_id} has processes from its last run that could not be stopped; "
                "run `meridian doctor`"
            )
    # A managed primary's backend and TUI belong to its session, whose runner is dead.
    metadata = read_primary_metadata(launch)
    terminate_managed_primary_processes(metadata, include_launcher=False)
    _sweep_scopes(launch, spawn_id)
    _require_managed_processes_gone(metadata, spawn_id)
    return row


def _require_managed_processes_gone(metadata: PrimaryMetadata | None, spawn_id: str) -> None:
    """The backend and TUI were signalled by exact birth; the next launch needs them gone."""
    if metadata is None or not metadata.managed_backend:
        return
    survivors = (
        (metadata.backend_pid, metadata.backend_birth_epoch),
        (metadata.tui_pid, metadata.tui_birth_epoch),
    )
    deadline = time.monotonic() + _TEARDOWN_GRACE_SECS
    while time.monotonic() < deadline:
        if not any(
            pid is not None and is_process_alive_with_birth(pid, birth) for pid, birth in survivors
        ):
            return
        time.sleep(0.05)
    raise ValueError(
        f"{spawn_id} has processes from its last run that could not be stopped; "
        "run `meridian doctor`"
    )


def _check_chat_free(runtime_root: Path, chat_id: str) -> None:
    """Advisory: ``start_session``'s lock is the authority, so this releases at once."""
    with try_lock_file(
        RuntimePaths.from_root_dir(runtime_root).sessions_dir / f"{chat_id}.lock",
        reentrant=False,
    ) as held:
        if held is None:
            raise ValueError(
                f"Chat {chat_id} is held by another session; continue after it exits, "
                f"or --fork {chat_id}"
            )


def begin_continue(
    *,
    project_root: Path,
    runtime_root: Path,
    target: SpawnRecord,
    chat_id: str | None,
    parent_id: str | None,
    caller_spawn_id: str | None,
    dry_run: bool,
) -> ContinueHold | None:
    """Steps 2-4 of a continue; ``None`` for ``dry_run``, which probes and writes nothing.

    ``target`` is ``resolve_continue_target``'s spawn and ``chat_id`` the chat to resume.
    ``parent_id`` is what the continue makes the spawn's parent: its caller, or none for a
    primary. Refuses before any write when the spawn is running (a
    repair or another continue holds the lock for seconds, so there is no retry), or when
    the last launch's processes cannot be stopped.
    """
    spawn_id = target.id
    if dry_run:
        lock_free = shared_try(lock_path(runtime_root, spawn_id))
        if lock_free is False:
            raise _busy_error(runtime_root, spawn_id, chat_id, caller_spawn_id)
        if lock_free is None and is_live(runtime_root, spawn_id):
            raise _legacy_live_error(spawn_id)
        if parent_id != target.parent_id:
            spawn_store.reject_continue_cycle(
                RuntimePaths.from_root_dir(runtime_root).spawns_dir, spawn_id, parent_id
            )
        return None
    try:
        handle = _acquire_source(runtime_root, spawn_id)
    except (Busy, Contended) as exc:
        raise _busy_error(runtime_root, spawn_id, chat_id, caller_spawn_id) from exc
    slot = LaunchSlot(handle)
    try:
        row = _finish_last_launch(project_root, runtime_root, handle)
        if parent_id != row.parent_id:
            spawn_store.reject_continue_cycle(
                RuntimePaths.from_root_dir(runtime_root).spawns_dir, spawn_id, parent_id
            )
        if chat_id:
            _check_chat_free(runtime_root, chat_id)
    except BaseException:
        slot.close()
        raise
    return ContinueHold(slot=slot, spawn=row)


__all__ = ["ContinueHold", "begin_continue", "resolve_continue_target"]
