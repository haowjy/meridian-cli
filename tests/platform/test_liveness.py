from __future__ import annotations

import os

import psutil

from meridian.lib.state import liveness


class _FakeProcess:
    def __init__(
        self,
        *,
        create_time: float = 100.0,
        is_running: bool = True,
        status: str = psutil.STATUS_RUNNING,
    ) -> None:
        self._create_time = create_time
        self._is_running = is_running
        self._status = status

    def create_time(self) -> float:
        return self._create_time

    def is_running(self) -> bool:
        return self._is_running

    def status(self) -> str:
        return self._status


def test_is_process_alive_returns_false_for_pid_reuse(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=131.0))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is False


def test_process_liveness_treats_zombie_as_exited(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(
        liveness.psutil,
        "Process",
        lambda pid: _FakeProcess(status=psutil.STATUS_ZOMBIE),
    )

    assert liveness.is_process_alive(123) is False
    assert liveness.is_process_alive_with_birth(123, 100.0) is False
    assert liveness.is_process_alive_born_by(123, 100.0) is False


def test_is_process_alive_returns_true_on_access_denied(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    def _raise_access_denied(pid: int):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(liveness.psutil, "Process", _raise_access_denied)

    assert liveness.is_process_alive(123) is True
    assert liveness.is_process_alive_born_by(123, 100.0) is False


def test_birth_liveness_tolerates_clock_steps_on_live_process() -> None:
    pid = os.getpid()
    birth = psutil.Process(pid).create_time()
    for shift in (-1.0, 1.0, 0.01):
        recorded_birth = birth + shift
        assert liveness.is_process_alive_with_birth(pid, recorded_birth) is True
        assert liveness.is_process_dead_with_birth(pid, recorded_birth) is False


def test_is_process_alive_with_birth_returns_false_for_birth_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=102.1))

    assert liveness.is_process_alive_with_birth(123, 100.0) is False


def test_is_process_alive_with_birth_fails_closed_for_unknown_birth(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    assert liveness.is_process_alive_with_birth(123, None) is False
    assert liveness.is_process_alive_with_birth(123, 0.0) is False
