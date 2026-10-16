"""Shared run-reopen steps for primary and child launchers."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.types import RunId
from meridian.lib.launch.request import SessionRequest, is_exact_continue_session
from meridian.lib.platform.locking import FileLockTimeout
from meridian.lib.state import spawn_store
from meridian.lib.state.session_store import ChatBusy
from meridian.lib.state.spawn_scope import read_spawn_scope, write_spawn_scope_task_dir


def _seed_continue_scope(project_root: Path, run_id: RunId, request: SessionRequest) -> None:
    """Copy the predecessor's run-local override, including an explicit clear."""
    if request.source_run_id and is_exact_continue_session(request):
        scope = read_spawn_scope(
            project_root, parse_ref(request.source_run_id, row_key=True)
        )
        if scope.task_dir is not None or scope.task_dir_cleared:
            write_spawn_scope_task_dir(project_root, run_id, scope.task_dir)


def reserve_reopened_run(
    lifecycle: SpawnLifecycleService,
    project_root: Path,
    reservation: SpawnReservation,
    request: SessionRequest,
) -> RunId:
    """Reserve a reopened run and copy its predecessor's portable scope."""
    if reservation.reopen_spawn_id is None:
        raise ValueError("A reopened-run reservation requires a stable spawn ID")
    run_id = parse_ref(lifecycle.reserve(reservation), row_key=True)
    _seed_continue_scope(project_root, run_id, request)
    return run_id


def bind_reopened_run(
    runtime_root: Path,
    run_id: RunId,
    *,
    work_id: str | None,
) -> None:
    """Attach the acquired chat's active work to a reopened run."""
    if work_id is not None:
        spawn_store.update_spawn(runtime_root, run_id, work_id=work_id)


def fail_unacquired_run(
    lifecycle: SpawnLifecycleService,
    run_id: RunId,
    error: Exception | str,
    *,
    exit_code: int = 1,
) -> None:
    """Finalize a reserved run when launch fails before a session is acquired."""
    if isinstance(error, ChatBusy):
        error_code = "chat_busy"
    elif isinstance(error, FileLockTimeout):
        error_code = "chat_lock_timeout"
    elif isinstance(error, FileNotFoundError):
        error_code = "pre_session_failed"
    else:
        error_code = str(error)
    lifecycle.finalize(
        run_id,
        status="failed",
        exit_code=exit_code,
        origin="launch_failure",
        error=error_code,
    )


__all__ = ["bind_reopened_run", "fail_unacquired_run", "reserve_reopened_run"]
