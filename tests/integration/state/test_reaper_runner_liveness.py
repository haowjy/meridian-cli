# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownParameterType=false, reportMissingParameterType=false
# qa-validated: test-suite-redesign

"""Runner liveness and runner-exit reconciliation behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.state import spawn_store
from tests.integration.state.conftest import (
    _OLD_STARTED_AT,
    _create_spawn,
    _get_spawn,
    _reconcile,
    _write_activity_artifact,
)


class _FakeRunnerProcess:
    def __init__(self, pid: int, *, create_time: float, running: bool = True) -> None:
        self.pid = pid
        self._create_time = create_time
        self._running = running

    def create_time(self) -> float:
        return self._create_time

    def is_running(self) -> bool:
        return self._running


def _patch_runner_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int,
    create_time: float,
    running: bool = True,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.state.liveness.psutil.pid_exists", lambda candidate: candidate == pid
    )
    monkeypatch.setattr(
        "meridian.lib.state.liveness.psutil.Process",
        lambda candidate: _FakeRunnerProcess(
            candidate,
            create_time=create_time,
            running=running,
        ),
    )
    # update_spawn auto-derives runner_created_at_epoch from the real OS when epoch
    # is None; stub it so PID-reuse tests never leak to live PIDs (Windows CI flake).
    monkeypatch.setattr(
        "meridian.lib.state.spawn_store._runner_created_at_epoch_for_pid",
        lambda _pid: None,
    )


@pytest.mark.parametrize(
    ("process_create_time", "expected_status", "expected_error"),
    [
        (230.0, "running", None),
        (253.0, "failed", "orphan_run"),
    ],
)
def test_reconcile_active_spawn_uses_recorded_runner_birth_for_pid_reuse_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_create_time: float,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runner_pid = 8123
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
        runner_pid=runner_pid,
    )
    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        runner_pid=runner_pid,
        runner_created_at_epoch=222.25,
    )
    _patch_runner_process(monkeypatch, pid=runner_pid, create_time=process_create_time)
    record = _get_spawn(runtime_root, spawn_id)

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.error == expected_error


@pytest.mark.parametrize(
    ("process_create_time", "expected_status", "expected_error"),
    [
        (946684810.0, "running", None),
        (946684831.0, "failed", "orphan_run"),
    ],
)
def test_reconcile_active_spawn_falls_back_to_started_epoch_for_pid_reuse_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_create_time: float,
    expected_status: str,
    expected_error: str | None,
) -> None:
    runner_pid = 8124
    _patch_runner_process(monkeypatch, pid=runner_pid, create_time=process_create_time)
    runtime_root, spawn_id = _create_spawn(
        tmp_path,
        started_at=_OLD_STARTED_AT,
        runner_pid=runner_pid,
    )
    spawn_store.update_spawn(
        runtime_root, spawn_id, runner_pid=runner_pid, runner_created_at_epoch=None
    )
    record = _get_spawn(runtime_root, spawn_id)
    assert record.runner_created_at_epoch is None

    reconciled = _reconcile(tmp_path, runtime_root, record)

    assert reconciled.status == expected_status
    assert reconciled.error == expected_error
    latest = _get_spawn(runtime_root, spawn_id)
    assert latest.status == expected_status
    assert latest.error == expected_error


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
