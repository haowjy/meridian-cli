"""Unit tests for _terminate_pid in managed_primary.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import psutil

from meridian.lib.state.managed_primary import _terminate_pid, terminate_managed_primary_processes
from meridian.lib.state.primary_meta import PrimaryMetadata


def test_rejects_zero_pid() -> None:
    assert _terminate_pid(0) is False
def test_rejects_own_pid() -> None:
    assert _terminate_pid(os.getpid()) is False


def test_terminates_and_returns_true() -> None:
    """_terminate_pid signals the correct process and returns True.

    Uses a stateful fake to verify the target PID received a terminate signal
    without pinning psutil call counts or constructor wiring.
    """
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self._pid = pid

        def terminate(self) -> None:
            terminated_pids.append(self._pid)

    with patch("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess):
        result = _terminate_pid(12345)

    assert result is True
    assert 12345 in terminated_pids  # terminate was called for the correct PID


def test_returns_false_on_no_such_process() -> None:
    mock_proc = MagicMock()
    mock_proc.terminate.side_effect = psutil.NoSuchProcess(12345)
    with patch("meridian.lib.state.managed_primary.psutil.Process", return_value=mock_proc):
        assert _terminate_pid(12345) is False


def test_terminate_managed_primary_processes_skips_birth_mismatch(monkeypatch) -> None:
    metadata = PrimaryMetadata(
        managed_backend=True,
        launcher_pid=9101,
        launcher_birth_epoch=100.0,
        backend_pid=9102,
        backend_birth_epoch=200.0,
        tui_pid=9103,
        tui_birth_epoch=None,
    )
    live_births = {9101: 101.2, 9102: 200.0, 9103: 300.0}
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return live_births[self.pid]

        def is_running(self) -> bool:
            return True

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.liveness.psutil.pid_exists", lambda pid: True)
    monkeypatch.setattr("meridian.lib.state.liveness.psutil.Process", _FakeProcess)
    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    signaled = terminate_managed_primary_processes(
        metadata,
        include_launcher=True,
        include_runtime_children=True,
    )

    assert signaled == (9102,)
    assert terminated_pids == [9102]


def test_terminate_managed_primary_processes_signals_only_exact_birth_match(monkeypatch) -> None:
    metadata = PrimaryMetadata(
        managed_backend=True,
        launcher_pid=9201,
        launcher_birth_epoch=120.0,
        backend_pid=9202,
        backend_birth_epoch=220.0,
        tui_pid=9203,
        tui_birth_epoch=320.0,
    )
    live_births = {9201: 120.0, 9202: 221.1, 9203: 320.0}
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return live_births[self.pid]

        def is_running(self) -> bool:
            return True

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

    monkeypatch.setattr("meridian.lib.state.liveness.psutil.pid_exists", lambda pid: True)
    monkeypatch.setattr("meridian.lib.state.liveness.psutil.Process", _FakeProcess)
    monkeypatch.setattr("meridian.lib.state.managed_primary.psutil.Process", _FakeProcess)

    signaled = terminate_managed_primary_processes(
        metadata,
        include_launcher=True,
        include_runtime_children=True,
    )

    assert signaled == (9201, 9203)
    assert terminated_pids == [9201, 9203]
