from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
from meridian.lib.core.domain import is_terminal_spawn_status
from meridian.lib.harness.plugin_hosted import is_plugin_hosted
from meridian.lib.platform.locking import lock_file
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
from meridian.lib.state.liveness import is_process_dead_with_birth
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.session_identity import chat_owner_spawn
from meridian.lib.state.session_store import chat_lock
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    read_state,
    spawn_lock_path,
)
from meridian.lib.state.spawn_status import is_live, legacy_runner_alive
from meridian.plugin_api.types import SpawnKind


@dataclass(frozen=True)
class ContinueHold:
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
    if row is not None and is_plugin_hosted(row.harness):
        # No runner to message and no fork to offer: its plugin's server holds the lock for
        # as long as the person's terminal has the conversation open.
        return ValueError(f"{spawn_id} is open in {row.harness}; close it there, then continue")
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
        f"{spawn_id} is still running; use meridian message {spawn_id} or --fork {spawn_id}"
    )


def resolve_continue_target(
    runtime_root: Path,
    named: SpawnRecord,
    *,
    named_by_spawn_ref: bool,
    chat_id: str | None,
    parent_id: str | None,
    kind: SpawnKind,
) -> tuple[SpawnRecord, str | None]:
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
        row = build_spawn_application_service_from_roots(
            project_root, runtime_root
        ).finish_last_launch(handle)
        if parent_id != row.parent_id:
            spawn_store.reject_continue_cycle(
                RuntimePaths.from_root_dir(runtime_root).spawns_dir, spawn_id, parent_id
            )
        if chat_id:
            # Refuse before the spawn's row is reopened; ``start_session`` takes it for real.
            with chat_lock(runtime_root, chat_id):
                pass
    except BaseException:
        slot.close()
        raise
    return ContinueHold(slot=slot, spawn=row)


__all__ = ["ContinueHold", "begin_continue", "resolve_continue_target"]
