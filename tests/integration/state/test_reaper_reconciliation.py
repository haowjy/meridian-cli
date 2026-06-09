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
    _write_report,
    fake_reaper_liveness,
    recording_scope_cleanup,
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
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

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
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

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
    fake_reaper_liveness(monkeypatch, set())

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
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "cancelled"
    assert reconciled.exit_code == 130
    assert reconciled.error == "cancelled"


def test_reconcile_active_spawn_with_cancel_intent_keeps_durable_completion(
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
    _write_report(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None


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


def test_reconcile_active_spawn_dead_runner_recent_activity_still_fails(
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
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


def test_reconcile_active_spawn_dead_runner_reaps_recorded_backend_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.process_scope_projection import record_scope

    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="child",
        harness="opencode",
        runner_pid=9301,
        worker_pid=9302,
        started_at=_OLD_STARTED_AT,
    )
    backend_scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="spawn_owned",
        owner_id=spawn_id,
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=9401,
        root_created_at_epoch=100.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(runtime_root, SpawnId(spawn_id), backend_scope)
    _write_activity_artifact(runtime_root, spawn_id, "heartbeat", age_secs=5)
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())
    terminated_scopes = recording_scope_cleanup(
        monkeypatch,
        "meridian.lib.core.process_cleanup.terminate_scope_sync",
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_run"
    assert terminated_scopes == ["backend:9401:reaper"]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


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
    fake_reaper_liveness(monkeypatch, set())

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
    fake_reaper_liveness(monkeypatch, lambda _pid: True)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled == record
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "running"
    assert latest.error is None


@pytest.mark.parametrize(
    ("runner_status", "runner_exit_code", "runner_error", "expected_status", "expected_error"),
    [
        ("succeeded", 0, None, "succeeded", None),
        ("failed", 23, "guardrail_failed", "failed", "guardrail_failed"),
        ("timed_out", 1, "resident_deadline_expired", "timed_out", "resident_deadline_expired"),
    ],
)
def test_reconcile_active_spawn_finalizes_from_runner_exit_tuple_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: str,
    runner_exit_code: int,
    runner_error: str | None,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runtime_root, spawn_id = _create_spawn(tmp_path, started_at=_OLD_STARTED_AT)
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status=runner_status,
        exit_code=runner_exit_code,
        error=runner_error,
        exited_at="1970-01-01T00:16:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.exit_code == runner_exit_code
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.exit_code == runner_exit_code
    assert latest.error == expected_error


def test_reconcile_active_spawn_durable_report_wins_over_cancelled_runner_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    fake_reaper_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "succeeded"
    assert latest.exit_code == 0
    assert latest.error is None
