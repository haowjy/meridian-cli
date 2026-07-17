"""Shared helpers for state integration tests.

Reaper helpers are used across test_reaper_reconciliation.py,
test_reaper_managed_primary.py, and test_reaper_cancel.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import record_launch_boundary_event
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.reaper import reconcile_active_spawn
from meridian.lib.state.spawn.model import LaunchMode, SpawnRecord

_OLD_STARTED_AT = "2000-01-01T00:00:00Z"


def _recent_started_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        status=status,
        runner_pid=runner_pid,
        started_at=started_at,
    )
    return runtime_root, str(created_spawn_id)


def _get_spawn(runtime_root: Path, spawn_id: str) -> SpawnRecord:
    record = spawn_store.get_spawn(runtime_root, spawn_id)
    assert record is not None
    return record


def _reconcile(project_root: Path, runtime_root: Path, record: SpawnRecord) -> SpawnRecord:
    return reconcile_active_spawn(project_root, runtime_root, record)


def _write_report(
    runtime_root: Path,
    spawn_id: str,
    text: str = "# Finished\n\nCompleted.\n",
) -> Path:
    report_path = runtime_root / "spawns" / spawn_id / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    return report_path


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
    metadata_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
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


def _write_corrupt_primary_meta(runtime_root: Path, spawn_id: str) -> Path:
    metadata_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text("{corrupt json", encoding="utf-8")
    return metadata_path


def _set_artifact_age_secs(path: Path, *, age_secs: float) -> None:
    target_epoch = time.time() - age_secs
    os.utime(path, (target_epoch, target_epoch))


def _write_activity_artifact(
    runtime_root: Path,
    spawn_id: str,
    artifact_name: str,
    *,
    age_secs: float,
) -> Path:
    artifact_path = runtime_root / "spawns" / spawn_id / artifact_name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if artifact_name == "heartbeat":
        artifact_path.touch()
    else:
        artifact_path.write_text("recent activity\n", encoding="utf-8")
    _set_artifact_age_secs(artifact_path, age_secs=age_secs)
    return artifact_path


def _record_launch_boundary(
    runtime_root: Path,
    spawn_id: str,
    *,
    event: str,
    launcher_pid: int | None = None,
    worker_pid: int | None = None,
) -> None:
    record_launch_boundary_event(
        runtime_root,
        spawn_id,
        event=event,
        launcher_pid=launcher_pid,
        worker_pid=worker_pid,
    )


def fake_reaper_liveness(
    monkeypatch,
    live_pids,
) -> None:
    """Patch runner liveness to a PID allowlist or predicate.

    Reaper reconciliation, managed-primary reconciliation, and nested read
    projection each consult ``is_process_alive`` via module-local bindings.
    Patch all three so fixture PIDs never fall through to the real OS.
    """

    def _is_alive(pid: int, created_after_epoch: float | None = None) -> bool:
        _ = created_after_epoch
        return live_pids(pid) if callable(live_pids) else pid in live_pids

    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", _is_alive)
    monkeypatch.setattr("meridian.lib.state.managed_primary.is_process_alive", _is_alive)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.is_process_alive", _is_alive)


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
