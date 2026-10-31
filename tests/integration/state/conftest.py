"""Shared helpers for state integration tests."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.spawn.model import LaunchMode, SpawnRecord
from tests.support.launch_paths import launch_paths

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _state_root(tmp_path: Path) -> Path:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _create_spawn(
    tmp_path: Path,
    *,
    spawn_id: str = "p1",
    status: SpawnStatus = "running",
    kind: str = "child",
    harness: str = "codex",
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = 123,
    started_at: str | None = _OLD_STARTED_AT,
) -> tuple[Path, str]:
    runtime_root = _state_root(tmp_path)
    initial_status = (
        SpawnStatus.RUNNING
        if status in {"succeeded", "failed", "cancelled", "timed_out"}
        else status
    )
    created_spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="gpt-5.4",
        agent="tester",
        harness=harness,
        kind=kind,
        prompt="hello",
        worker_pid=worker_pid,
        launch_mode=launch_mode,
        status=initial_status,
        runner_pid=runner_pid,
        started_at=started_at,
    ).spawn_id
    if status in {"succeeded", "failed", "cancelled", "timed_out"}:
        spawn_store.finalize_spawn(
            runtime_root,
            created_spawn_id,
            status=status,  # type: ignore[arg-type]
            exit_code=0 if status == "succeeded" else 1,
            origin="runner",
        )
    return runtime_root, str(created_spawn_id)


def _get_spawn(runtime_root: Path, spawn_id: str) -> SpawnRecord:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def _reconcile(project_root: Path, runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    return reconcile_active_spawn(project_root, runtime_root, record)


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    launcher_pid: int | None,
    launcher_birth_epoch: float | None = None,
    backend_pid: int | None = None,
    backend_birth_epoch: float | None = None,
    tui_pid: int | None = None,
    tui_birth_epoch: float | None = None,
    activity: str = "idle",
    managed_backend: bool = True,
) -> Path:
    metadata_path = launch_paths(runtime_root, spawn_id).primary_meta
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "managed_backend": managed_backend,
                "launcher_pid": launcher_pid,
                "launcher_birth_epoch": launcher_birth_epoch,
                "backend_pid": backend_pid,
                "backend_birth_epoch": backend_birth_epoch,
                "tui_pid": tui_pid,
                "tui_birth_epoch": tui_birth_epoch,
                "activity": activity,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return metadata_path


def fake_managed_primary_birth_liveness(
    monkeypatch,
    live_pids,
) -> None:
    """Patch managed-primary birth-checked liveness to a PID allowlist or predicate."""

    def _is_alive(pid: int, birth_epoch: float | None) -> bool:
        _ = birth_epoch
        return live_pids(pid) if callable(live_pids) else pid in live_pids

    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive_with_birth",
        _is_alive,
    )


def recording_managed_primary_terminations(monkeypatch) -> list[int]:
    """Record managed-primary termination attempts without touching real processes."""

    from meridian.lib.platform.process_scope.base import CleanupResult

    terminated_pids: list[int] = []

    def _terminate(pid: int) -> bool:
        terminated_pids.append(pid)
        return True

    def _terminate_scope(scope, *, grace_seconds: float, reason: str) -> CleanupResult:
        terminated_pids.append(scope.root_pid)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    monkeypatch.setattr("meridian.lib.state.managed_primary._terminate_pid", _terminate)
    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
        _terminate_scope,
    )
    return terminated_pids


def recording_scope_cleanup(monkeypatch, target: str) -> list[int | str]:
    """Patch a cleanup function and return the PID/scope calls it receives."""

    from meridian.lib.platform.process_scope.base import CleanupResult

    calls: list[int | str] = []

    def _cleanup(*args, **kwargs):
        subject = args[0] if args else kwargs.get("pid") or kwargs.get("scope")
        reason = kwargs.get("reason", "stop_called")
        if hasattr(subject, "scope_id"):
            calls.append(f"{subject.scope_id}:{subject.root_pid}:{reason}")
            return CleanupResult(
                scope_id=subject.scope_id,
                root_pid=subject.root_pid,
                descendant_count=0,
                reason=reason,
                grace_seconds=kwargs.get("grace_seconds", 5.0),
                kill_escalated=False,
                degraded_fallback=False,
                skip_reason=None,
            )
        calls.append(subject)
        return CleanupResult(
            scope_id=kwargs.get("scope_id", ""),
            root_pid=subject,
            descendant_count=0,
            reason=reason,
            grace_seconds=kwargs.get("grace_secs", 5.0),
            kill_escalated=False,
            degraded_fallback=kwargs.get("degraded_fallback", False),
            skip_reason=None,
        )

    monkeypatch.setattr(target, _cleanup)
    if target == "meridian.lib.core.process_cleanup.terminate_tree_sync":

        def _cleanup_claimed(scope, *, grace_seconds: float, reason: str):
            calls.append(scope.root_pid)
            return CleanupResult(
                scope_id=scope.scope_id,
                root_pid=scope.root_pid,
                descendant_count=0,
                reason=reason,
                grace_seconds=grace_seconds,
                kill_escalated=False,
                degraded_fallback=True,
                skip_reason=None,
            )

        monkeypatch.setattr(
            "meridian.lib.core.process_cleanup.terminate_scope_sync",
            _cleanup_claimed,
        )
    return calls
