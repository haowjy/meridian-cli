# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Reconciliation status-transition tests for reconcile_active_spawn.

Covers: terminal short-circuit, runner-pid guards, background-mode
boundary events, dead-runner + activity artifact heuristics, and the
MERIDIAN_DEPTH gate.  Managed-primary paths live in
test_reaper_managed_primary.py; cancel flows in test_reaper_cancel.py.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from meridian.lib.core.lifecycle import create_lifecycle_service as make_lifecycle_service
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_boundary import (
    EVENT_PARENT_LAUNCH_SPAWNED,
    EVENT_WORKER_TAKEOVER_STARTED,
)
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _recent_started_at,
    _reconcile,
    _record_launch_boundary,
    _set_artifact_age_secs,
    _write_activity_artifact,
    _write_report,
)


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


def test_reconcile_active_spawn_post_exit_failure_bypasses_recent_activity_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = 1_000.0
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    monkeypatch.setattr("tests.integration.state.conftest.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=3,
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_activity_artifact(runtime_root, spawn_id, "history.jsonl", age_secs=1)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 3
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.exit_code == 3
    assert latest.error == "orphan_run"


def test_reconcile_active_spawn_post_exit_zero_succeeds_after_grace_without_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = 1_000.0
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=0,
        exited_at="1970-01-01T00:16:30Z",
    )
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
