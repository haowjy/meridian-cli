# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Reconciliation status-transition tests for reconcile_active_spawn.

Covers: terminal short-circuit, runner-pid guards, background-mode
boundary events, dead-runner + activity artifact heuristics, and the
MERIDIAN_DEPTH gate.  Managed-primary paths live in
test_reaper_managed_primary.py; cancel flows in test_reaper_cancel.py.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    _write_activity_artifact,
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
def test_reconcile_active_spawn_finalizing_recent_activity_skips(
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
        age_secs=5,
    )
    record = _get_spawn(runtime_root, spawn_id)
    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "finalizing"
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


def test_reconcile_active_spawn_with_cancel_intent_and_dead_runner_cancels(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "cancelled"
    assert reconciled.exit_code == 130
    assert reconciled.error == "cancelled"
@pytest.mark.parametrize(
    ("depth_value", "expected_status", "expected_error"),
    [
        ("1", "running", None),
        ("0", "failed", "missing_runner_pid"),
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
def test_reconcile_active_spawn_dead_runner_recent_activity_skips_across_artifact_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    record = _get_spawn(runtime_root, spawn_id)
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
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


def test_reconcile_active_spawn_last_attempt_exit_drives_orphan_failure_after_activity_stales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    monkeypatch.setattr("tests.integration.state.conftest.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=3,
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_activity_artifact(runtime_root, spawn_id, "history.jsonl", age_secs=300)
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


def test_reconcile_active_spawn_post_exit_with_live_runner_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded attempt exit must not finalize a spawn whose runner is alive.

    The runner records last_attempt_exit_code/last_attempt_exited_at after every attempt drains,
    including between retries and before post-attempt guardrails run. While the
    runner process is still alive it owns finalization, so the reaper must skip
    rather than orphan it.
    """
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)

    fixed_now = time.time()
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: fixed_now)
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=1,
        exited_at="1970-01-01T00:00:30Z",
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
def test_reconcile_active_spawn_finalizes_from_runner_exit_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        error=None,
        exited_at="1970-01-01T00:16:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
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
def test_reconcile_active_spawn_finalizes_failed_runner_exit_tuple_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=23,
        error="guardrail_failed",
        exited_at="1970-01-01T00:16:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 23
    assert reconciled.error == "guardrail_failed"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.exit_code == 23
    assert latest.error == "guardrail_failed"


def test_reconcile_active_spawn_durable_report_wins_over_cancelled_runner_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.state.conftest import _write_report

    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="cancelled",
        exit_code=130,
        error="cancelled",
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_report(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
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


def test_reconcile_active_spawn_uses_runner_created_epoch_for_liveness_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
        runner_pid=8123,
    )
    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        runner_pid=8123,
        runner_created_at_epoch=222.25,
    )
    observed: dict[str, float | None] = {}

    def _capture_alive(pid: int, created_after_epoch: float | None = None) -> bool:
        _ = pid
        observed["created_after_epoch"] = created_after_epoch
        return False

    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", _capture_alive)
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert observed["created_after_epoch"] == 222.25
    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_run"


def test_reconcile_active_spawn_falls_back_to_started_epoch_when_runner_created_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force psutil-backed create-time lookup to return None so the test exercises
    # the started_epoch fallback regardless of whether the fake runner PID happens
    # to exist on the host (it doesn't on POSIX CI runners, but may on Windows).
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store._runner_created_at_epoch_for_pid",
        lambda _pid: None,
    )
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
        runner_pid=8124,
    )
    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        runner_pid=8124,
        runner_created_at_epoch=0.0,
    )
    spawn_store.update_spawn(runtime_root, spawn_id, runner_pid=8124, runner_created_at_epoch=None)
    observed: dict[str, float | None] = {}

    def _capture_alive(pid: int, created_after_epoch: float | None = None) -> bool:
        _ = pid
        observed["created_after_epoch"] = created_after_epoch
        return False

    monkeypatch.setattr("meridian.lib.state.reaper.is_process_alive", _capture_alive)
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    expected_started_epoch = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
    assert observed["created_after_epoch"] == expected_started_epoch
    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_run"


def test_reconcile_active_spawn_finalizing_uses_runner_exit_tuple(
    tmp_path: Path,
) -> None:
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        status="finalizing",
        started_at=_OLD_STARTED_AT,
    )
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="cancelled",
        exit_code=130,
        error="cancelled",
        exited_at="1970-01-01T00:16:30Z",
    )
    _write_activity_artifact(
        runtime_root,
        spawn_id,
        "heartbeat",
        age_secs=300,
    )
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "cancelled"
    assert reconciled.exit_code == 130
    assert reconciled.error == "cancelled"


def test_reconcile_active_spawn_runner_exit_grace_skips_until_grace_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=2,
        error="timeout",
        exited_at="1970-01-01T00:16:38Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
