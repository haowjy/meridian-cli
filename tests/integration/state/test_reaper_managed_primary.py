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

import pytest

from meridian.lib.state import spawn_store
from meridian.lib.state.managed_primary import terminate_managed_primary_processes
from meridian.lib.state.primary_meta import PrimaryMetadata
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    _set_artifact_age_secs,
    _write_corrupt_primary_meta,
    _write_primary_meta,
    _write_report,
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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    signaled = terminate_managed_primary_processes(
        metadata,
        started_epoch=100.0,
        include_launcher=False,
    )

    assert signaled == (8002,)
    assert terminated_pids == [8002]


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


def test_reconcile_active_spawn_managed_primary_finalizing_without_runner_exit_marks_orphan(
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

    assert reconciled.status == "failed"
    assert reconciled.error == "orphan_finalization"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_finalization"


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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert terminated_pids == [8882, 9992]


@pytest.mark.parametrize("harness", ["codex", "opencode"])
@pytest.mark.parametrize("corrupt_primary_meta", [False, True])
def test_reconcile_active_spawn_managed_primary_candidate_unreadable_metadata_kills_worker_pg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    corrupt_primary_meta: bool,
) -> None:
    from meridian.lib.platform.process_scope.base import CleanupResult

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
    terminated_pids: list[int] = []

    def _fake_terminate_tree_sync(
        pid: int,
        *,
        created_at_epoch: float = 0.0,
        grace_secs: float = 5.0,
        reason: str = "stop_called",
        scope_id: str = "",
        degraded_fallback: bool = False,
    ) -> CleanupResult:
        terminated_pids.append(pid)
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_primary"
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_primary"
    assert terminated_pids == [worker_pid]


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
    # Check the diagnostic warning (first emitted) which carries launcher_alive details
    event, payload = warnings[0]
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
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

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
    monkeypatch.setattr(
        "meridian.lib.state.reaper.is_process_alive",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "meridian.lib.state.managed_primary.is_process_alive",
        lambda *_args, **_kwargs: False,
    )

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
    from meridian.lib.platform.process_scope.base import CleanupResult

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
    terminated_pids: list[int] = []

    def _fake_terminate_tree_sync(
        pid: int,
        *,
        created_at_epoch: float = 0.0,
        grace_secs: float = 5.0,
        reason: str = "stop_called",
        scope_id: str = "",
        degraded_fallback: bool = False,
    ) -> CleanupResult:
        terminated_pids.append(pid)
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    monkeypatch.setattr(
        "meridian.lib.core.process_cleanup.terminate_tree_sync",
        _fake_terminate_tree_sync,
    )

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == "failed"
    assert reconciled.exit_code == 1
    assert reconciled.error == "orphan_run"
    assert terminated_pids == [worker_pid]
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == "failed"
    assert latest.error == "orphan_run"
