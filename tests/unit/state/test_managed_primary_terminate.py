"""Unit tests for _terminate_pid in managed_primary.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import psutil

from meridian.lib.state.managed_primary import _terminate_pid


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
