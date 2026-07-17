# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Managed-primary detection, PID validation, and orphan termination tests.

Covers: terminate_managed_primary_processes, reconcile_active_spawn paths
that touch primary_meta (idle/finalizing launcher alive, dead launcher
orphan, unreadable metadata fallback, report-recovery).  General
reconciliation lives in test_reaper_reconciliation.py; cancel flows in
test_reaper_cancel.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.platform.process_scope.base import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state import spawn_store
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    _set_artifact_age_secs,
    _write_corrupt_primary_meta,
    _write_primary_meta,
    _write_report,
    fake_managed_primary_birth_liveness,
    fake_reaper_liveness,
)

if TYPE_CHECKING:
    import pytest


def _record_claimed_scope_terminations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    terminated: list[int] = []

    def terminate(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> CleanupResult:
        terminated.append(scope.root_pid)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=scope.degraded_reason is not None,
            skip_reason=None,
        )

    monkeypatch.setattr("meridian.lib.core.process_cleanup.terminate_scope_sync", terminate)
    return terminated


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
    fake_reaper_liveness(monkeypatch, set())
    fake_managed_primary_birth_liveness(monkeypatch, {8882, 9992})
    terminated_pids = _record_claimed_scope_terminations(monkeypatch)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert terminated_pids == [8882, 9992]


def test_reconcile_active_spawn_managed_primary_candidate_unreadable_metadata_kills_worker_pg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_pid = 9101
    worker_pid = 9102
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        kind="primary",
        harness="codex",
        runner_pid=runner_pid,
        worker_pid=worker_pid,
        started_at=_OLD_STARTED_AT,
    )
    _write_corrupt_primary_meta(runtime_root, spawn_id)
    record = _get_spawn(runtime_root, spawn_id)

    fake_reaper_liveness(monkeypatch, {worker_pid})
    terminated_pids = _record_claimed_scope_terminations(monkeypatch)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert terminated_pids == [worker_pid]


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
    fake_reaper_liveness(monkeypatch, set())
    fake_managed_primary_birth_liveness(monkeypatch, set())
    terminated_pids = _record_claimed_scope_terminations(monkeypatch)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "succeeded"
    assert reconciled.exit_code == 0
    assert reconciled.error is None
    assert terminated_pids == []


def test_reconcile_active_spawn_managed_primary_uses_runner_exit_tuple_before_orphan_logic(
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
        launcher_pid=7775,
        backend_pid=8885,
        tui_pid=9995,
        activity="idle",
    )
    spawn_store.record_runner_exit(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=73,
        error="guardrail_failed",
        exited_at="1970-01-01T00:16:30Z",
    )
    record = _get_spawn(runtime_root, spawn_id)
    monkeypatch.setattr("meridian.lib.state.reaper.time.time", lambda: 1_000.0)
    fake_reaper_liveness(monkeypatch, set())
    fake_managed_primary_birth_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 73
    assert reconciled.error == "guardrail_failed"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.exit_code == 73
    assert latest.error == "guardrail_failed"


def test_reconcile_active_spawn_child_orphan_terminates_worker_process_group(
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
    fake_reaper_liveness(monkeypatch, {worker_pid})
    terminated_pids = _record_claimed_scope_terminations(monkeypatch)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    assert terminated_pids == [worker_pid]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"


def test_reconcile_managed_primary_finalizing_cancel_intent_cancels(
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
    _write_primary_meta(
        runtime_root,
        spawn_id,
        launcher_pid=7776,
        activity="finalizing",
    )
    record = _get_spawn(runtime_root, spawn_id)
    fake_reaper_liveness(monkeypatch, set())
    fake_managed_primary_birth_liveness(monkeypatch, set())

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "cancelled"
    assert reconciled.exit_code == 130
    assert reconciled.error == "cancelled"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "cancelled"
