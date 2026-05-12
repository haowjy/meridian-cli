"""Tests for ClaudeConnection cancel behavior across platforms."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import patch

from meridian.lib.harness.connections import claude_ws
from meridian.lib.harness.connections.claude_ws import ClaudeConnection


class _FakeProcess:
    """Minimal fake asyncio.subprocess.Process for cancel tests."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_called = False
        self.send_signal_calls: list[signal.Signals] = []
        self.stdin = None
        self.stdout = None

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = -1

    def send_signal(self, sig: signal.Signals) -> None:
        self.send_signal_calls.append(sig)


def _make_connection_with_process(fake_process: _FakeProcess) -> ClaudeConnection:
    conn = ClaudeConnection()
    conn._process = fake_process  # type: ignore[assignment]
    conn._state = "connected"
    return conn


def test_signal_process_windows_uses_terminate() -> None:
    """On Windows, _signal_process must call process.terminate() not send_signal."""
    fake = _FakeProcess()
    conn = _make_connection_with_process(fake)

    with patch.object(claude_ws, "IS_WINDOWS", True):
        asyncio.run(conn._signal_process(signal.SIGINT))

    assert fake.terminate_called, "terminate() must be called on Windows"
    assert fake.send_signal_calls == [], "send_signal must not be called on Windows"


def test_signal_process_posix_uses_send_signal() -> None:
    """On POSIX, _signal_process must call process.send_signal(SIGINT) not terminate."""
    fake = _FakeProcess()
    conn = _make_connection_with_process(fake)

    with patch.object(claude_ws, "IS_WINDOWS", False):
        asyncio.run(conn._signal_process(signal.SIGINT))

    assert not fake.terminate_called, "terminate() must not be called on POSIX"
    assert fake.send_signal_calls == [signal.SIGINT], "send_signal(SIGINT) must be called on POSIX"


def test_signal_process_no_op_when_process_none() -> None:
    """_signal_process is a no-op when there is no subprocess."""
    conn = ClaudeConnection()
    # _process is None by default — should not raise
    with patch.object(claude_ws, "IS_WINDOWS", False):
        asyncio.run(conn._signal_process(signal.SIGINT))


def test_signal_process_no_op_when_already_exited() -> None:
    """_signal_process is a no-op when the process has already exited."""
    fake = _FakeProcess()
    fake.returncode = 0  # already exited
    conn = _make_connection_with_process(fake)

    with patch.object(claude_ws, "IS_WINDOWS", False):
        asyncio.run(conn._signal_process(signal.SIGINT))

    assert fake.send_signal_calls == [], "send_signal must not be called after process exit"
    assert not fake.terminate_called, "terminate must not be called after process exit"
