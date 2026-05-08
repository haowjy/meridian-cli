# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import create_lifecycle_service as make_lifecycle_service
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.ops.spawn.models import SpawnCancelInput
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_SPAWNED,
    EVENT_WORKER_TAKEOVER_STARTED,
    record_launch_boundary_event,
)
from meridian.lib.state.managed_primary import terminate_managed_primary_processes
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.primary_meta import PrimaryMetadata
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
    backend_pid: int | None = None,
    tui_pid: int | None = None,
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
                "backend_pid": backend_pid,
                "tui_pid": tui_pid,
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


def test_terminate_managed_primary_processes_skips_unvalidated_child_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = PrimaryMetadata(
        managed_backend=True,
        launcher_pid=8001,
        backend_pid=8002,
        tui_pid=8003,
        activity="idle",
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid == 8002,
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    signaled = terminate_managed_primary_processes(
        metadata,
        started_epoch=100.0,
        include_launcher=False,
    )

    assert signaled == (8002,)
    assert sent_signals == [(8002, signal.SIGTERM)]


def test_reconcile_active_spawn_returns_terminal_record_unchanged(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, status="succeeded")
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    assert _get_spawn(runtime_root, spawn_id).status == "succeeded"


def test_reconcile_active_spawn_without_runner_pid_stays_unchanged_during_startup_grace(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=None,
        started_at=_recent_started_at(),
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_without_runner_pid_fails_after_startup_grace(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, runner_pid=None, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "missing_runner_pid"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "missing_runner_pid"


def test_reconcile_active_spawn_uses_authority_project_root_for_lifecycle_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project-root"
    runtime_root = tmp_path / "detached-runtime"
    project_root.mkdir()
    runtime_root.mkdir()
    spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p1",
            chat_id="c1",
            model="gpt-5.4",
            agent="tester",
            harness="codex",
            kind="child",
            prompt="hello",
            status="running",
            runner_pid=None,
            started_at=_OLD_STARTED_AT,
        )
    )
    record = _get_spawn(runtime_root, spawn_id)
    captured: dict[str, Path] = {}
    def _capture_factory(captured_project_root: Path, captured_runtime_root: Path):
        captured["project_root"] = captured_project_root
        captured["runtime_root"] = captured_runtime_root
        service = make_lifecycle_service(captured_project_root, captured_runtime_root)
        from meridian.lib.core.spawn_service import SpawnApplicationService

        return SpawnApplicationService(captured_runtime_root, service)

    monkeypatch.setattr(
        "meridian.lib.state.reaper.build_spawn_application_service_from_roots",
        _capture_factory,
    )

    reconciled = _reconcile(project_root, runtime_root, record)

    assert reconciled.status == "failed"
    assert captured == {
        "project_root": project_root,
        "runtime_root": runtime_root,
    }


def test_reconcile_active_spawn_background_without_takeover_evidence_gets_boundary_error(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        launch_mode="background",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        launcher_pid=8111,
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "launch_boundary_no_takeover"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "launch_boundary_no_takeover"


def test_reconcile_active_spawn_background_pid_collision_without_takeover_evidence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_pid = 8123
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        launch_mode="background",
        runner_pid=launcher_pid,
        started_at=_OLD_STARTED_AT,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        launcher_pid=launcher_pid,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: True,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "launch_boundary_no_takeover"


def test_reconcile_active_spawn_background_takeover_evidence_keeps_runner_alive_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher_pid = 8124
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        launch_mode="background",
        runner_pid=launcher_pid,
        started_at=_OLD_STARTED_AT,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_PARENT_LAUNCH_SPAWNED,
        launcher_pid=launcher_pid,
    )
    _record_launch_boundary(
        runtime_root,
        spawn_id,
        event=EVENT_WORKER_TAKEOVER_STARTED,
        worker_pid=9001,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: True,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_returns_unchanged_when_runner_is_alive(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: True,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_managed_primary_idle_launcher_alive_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7771,
        activity="idle",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid == 7771,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_managed_primary_launcher_alive_skips_when_finalizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7774,
        activity="finalizing",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid == 7774,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
    assert latest.error is None


def test_reconcile_active_spawn_managed_primary_dead_launcher_marks_orphan_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7772,
        backend_pid=8882,
        tui_pid=9992,
        activity="idle",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid in {8882, 9992},
    )
    sent_signals: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        sent_signals.append((pid, sig))

    monkeypatch.setattr("meridian.lib.state.managed_primary.os.kill", _fake_kill)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert sent_signals == []


@pytest.mark.parametrize("harness", ["codex", "opencode"])
@pytest.mark.parametrize("corrupt_primary_meta", [False, True])
def test_reconcile_active_spawn_managed_primary_candidate_unreadable_metadata_skips_worker_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    corrupt_primary_meta: bool,
) -> None:
    runner_pid = 9101
    worker_pid = 9102
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        harness=harness,
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    if corrupt_primary_meta:
        _write_corrupt_primary_meta(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)

    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda pid, created_after_epoch=None: pid == worker_pid,
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.reaper.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert sent_signals == []


def test_reconcile_active_spawn_orphan_primary_diagnostics_include_launcher_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_pid = 9201
    worker_pid = 9202
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        harness="codex",
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda pid, created_after_epoch=None: pid == worker_pid,
    )

    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "meridian.lib.state.reaper.logger.warning",
        lambda event, **kwargs: warnings.append((event, kwargs)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_primary"
    assert warnings
    event, payload = warnings[-1]
    assert "Managed primary candidate reconciled without readable metadata" in event
    assert payload["launcher_alive"] is False
    assert payload["managed_metadata_readable"] is False


def test_reconcile_active_spawn_managed_primary_finalizing_activity_uses_report_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
    )
    report_path = _write_report(runtime_root, spawn_id)
    _set_artifact_age_secs(report_path, age_secs=300)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7773,
        backend_pid=8883,
        tui_pid=9993,
        activity="finalizing",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None
    assert sent_signals == []


def test_reconcile_active_spawn_finalizing_stale_heartbeat_marks_orphan_finalization(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=300,
    )
    record = _get_spawn(runtime_root, spawn_id)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_finalization"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_finalization"


@pytest.mark.parametrize("artifact_name", ["heartbeat", "stderr.log"])
def test_reconcile_active_spawn_finalizing_recent_activity_skips(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        artifact_name,
        age_secs=5,
    )
    record = _get_spawn(runtime_root, spawn_id)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
    assert latest.error is None


def test_reconcile_active_spawn_with_dead_runner_and_report_succeeds_without_exit_event(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    report_path = _write_report(runtime_root, spawn_id)
    _set_artifact_age_secs(report_path, age_secs=300)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "succeeded"
    assert latest.exit_code == 0
    assert latest.error is None


def test_reconcile_active_spawn_with_dead_runner_and_no_exit_or_report_fails(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


@pytest.mark.parametrize(
    ("depth_value", "expected_status", "expected_error"),
    [
        ("1", "running", None),
        ("0", "failed", "missing_runner_pid"),
        ("garbage", "running", None),
        ("1.5", "running", None),
        ("-1", "running", None),
    ],
)
def test_reconcile_active_spawn_depth_gate_respects_env_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth_value: str,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setenv("MERIDIAN_DEPTH", depth_value)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.error == expected_error
    if expected_status == "failed":
        assert reconciled.exit_code == 1
        assert latest.exit_code == 1


def test_reconcile_active_spawn_treats_exact_heartbeat_window_boundary_as_recent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    heartbeat_path = runtime_root / "spawns" / spawn_id / "heartbeat"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.touch()

    fixed_now = 1_000.0
    os.utime(heartbeat_path, (fixed_now - 120.0, fixed_now - 120.0))
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


@pytest.mark.parametrize("artifact_name", ["heartbeat", "history.jsonl", "stderr.log", "report.md"])
def test_reconcile_active_spawn_dead_runner_recent_activity_skips_across_artifact_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        artifact_name,
        age_secs=5,
    )

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


def test_reconcile_active_spawn_child_orphan_terminates_worker_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_pid = 9301
    worker_pid = 9302
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="child",
        harness="codex",
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda pid, created_after_epoch=None: pid == worker_pid,
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.reaper.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    assert sent_signals == [(worker_pid, signal.SIGTERM)]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


def _patch_spawn_cancel_runtime_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_root: Path,
    spawn_id: str,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_runtime_root_and_config",
        lambda _project_root: (runtime_root, object()),
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_runtime_root",
        lambda _project_root: runtime_root,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.spawn.api.resolve_spawn_reference",
        lambda _project_root, _spawn_id: spawn_id,
    )


def test_cancel_orphan_primary_after_passive_reconcile_still_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7301,
        backend_pid=7302,
        tui_pid=7303,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid in {7302, 7303},
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    assert sent_signals == []

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert output.message == f"Spawn '{spawn_id}' is already failed."
    assert sent_signals == [(7302, signal.SIGTERM), (7303, signal.SIGTERM)]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"


@pytest.mark.parametrize("harness", ["codex", "opencode"])
@pytest.mark.parametrize("corrupt_primary_meta", [False, True])
def test_cancel_orphan_primary_candidate_with_unreadable_metadata_uses_worker_pid_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    corrupt_primary_meta: bool,
) -> None:
    runner_pid = 9351
    worker_pid = 9352
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        harness=harness,
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    if corrupt_primary_meta:
        _write_corrupt_primary_meta(runtime_root, spawn_id)

    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda pid, created_after_epoch=None: pid == worker_pid,
    )
    passive_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.reaper.os.kill",
        lambda pid, sig: passive_signals.append((pid, sig)),
    )

    reconciled = _reconcile(tmp_path, runtime_root, _get_spawn(runtime_root, spawn_id))

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    assert passive_signals == []

    explicit_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda pid, created_after_epoch=None: pid == worker_pid,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.os.kill",
        lambda pid, sig: explicit_signals.append((pid, sig)),
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert output.message == f"Spawn '{spawn_id}' is already failed."
    assert explicit_signals == [(worker_pid, signal.SIGTERM)]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"


def test_cancel_terminal_non_orphan_primary_does_not_terminate_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="succeeded",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7401,
        backend_pid=7402,
        tui_pid=7403,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "succeeded"
    assert output.exit_code == 1
    assert output.message == f"Spawn '{spawn_id}' is already succeeded."
    assert sent_signals == []


def test_spawn_cancel_managed_primary_signals_launcher_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7001,
        backend_pid=7002,
        tui_pid=7003,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda pid, created_after_epoch=None: pid == 7001,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid in {7001, 7002, 7003},
    )
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "finalizing"
    assert output.exit_code == 1
    assert sent_signals[0] == (7001, signal.SIGTERM)
    assert set(sent_signals[1:]) == {(7002, signal.SIGTERM), (7003, signal.SIGTERM)}
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"


def test_spawn_cancel_managed_primary_without_launcher_directly_terminates_backend_and_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7101,
        backend_pid=7102,
        tui_pid=7103,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda pid, created_after_epoch=None: pid in {7102, 7103},
    )
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "finalizing"
    assert output.exit_code == 1
    assert sent_signals == [(7102, signal.SIGTERM), (7103, signal.SIGTERM)]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"


def test_spawn_cancel_managed_primary_queued_converges_to_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="queued",
        runner_pid=None,
        started_at=_OLD_STARTED_AT,
    )
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7201,
        backend_pid=7202,
        tui_pid=7203,
        activity="idle",
    )
    _patch_spawn_cancel_runtime_resolution(
        monkeypatch,
        runtime_root=runtime_root,
        spawn_id=spawn_id,
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_GRACE_SECS", 0.01)
    monkeypatch.setattr("meridian.lib.core.spawn_service._MANAGED_CANCEL_FALLBACK_WAIT_SECS", 0.01)
    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.os.kill",
        lambda pid, sig: sent_signals.append((pid, sig)),
    )

    output = spawn_api.spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
            project_root=tmp_path.as_posix(),
        )
    )

    assert output.status == "failed"
    assert output.exit_code == 1
    assert sent_signals == []
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "cancel_timeout"
    assert latest.terminal_origin == "cancel"
