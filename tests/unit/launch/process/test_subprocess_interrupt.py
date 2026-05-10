"""Tests for _wait_for_process KeyboardInterrupt handling."""

from __future__ import annotations

import pytest

from meridian.lib.launch.process.subprocess_launcher import _wait_for_process


class _FakeProcess:
    """Minimal Popen stand-in for interrupt handler tests."""

    def __init__(
        self,
        *,
        running: bool,
        wait_exit_code: int = 0,
        raise_interrupt: bool = False,
    ) -> None:
        self._running = running
        self._wait_exit_code = wait_exit_code
        self._raise_interrupt = raise_interrupt
        self._wait_calls = 0
        self.terminate_called = False
        self.send_signal_called = False

    def poll(self) -> int | None:
        return None if self._running else self._wait_exit_code

    def wait(self) -> int:
        self._wait_calls += 1
        if self._raise_interrupt and self._wait_calls == 1:
            raise KeyboardInterrupt
        return self._wait_exit_code

    def terminate(self) -> None:
        self.terminate_called = True

    def send_signal(self, _sig: object) -> None:
        self.send_signal_called = True


@pytest.mark.unit
def test_interrupt_while_running_calls_terminate() -> None:
    """KeyboardInterrupt with live process: terminate() is called, not send_signal."""
    process = _FakeProcess(running=True, wait_exit_code=1, raise_interrupt=True)

    result = _wait_for_process(process)  # type: ignore[arg-type]

    assert process.terminate_called is True
    assert process.send_signal_called is False
    assert result == 1


@pytest.mark.unit
def test_interrupt_while_already_exited_returns_130() -> None:
    """KeyboardInterrupt after process exits: return 130 without terminate."""
    process = _FakeProcess(running=False, wait_exit_code=0, raise_interrupt=True)

    result = _wait_for_process(process)  # type: ignore[arg-type]

    assert result == 130
    assert process.terminate_called is False
    assert process.send_signal_called is False


@pytest.mark.unit
def test_normal_exit_returns_exit_code() -> None:
    """Happy path: wait() returns the process exit code directly."""
    process = _FakeProcess(running=True, wait_exit_code=42)

    result = _wait_for_process(process)  # type: ignore[arg-type]

    assert result == 42
    assert process.terminate_called is False
