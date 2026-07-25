from __future__ import annotations

import os
import time
from types import SimpleNamespace

import psutil
import pytest

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


def test_is_process_alive_returns_false_when_pid_does_not_exist(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: False)

    assert liveness.is_process_alive(123) is False


def test_is_process_alive_returns_false_for_pid_reuse(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=131.0))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is False
def test_is_process_alive_returns_process_running_state(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(is_running=True))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is True


def test_process_liveness_treats_zombie_as_exited(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(
        liveness.psutil,
        "Process",
        lambda pid: _FakeProcess(status=psutil.STATUS_ZOMBIE),
    )

    assert liveness.is_process_alive(123) is False
    assert liveness.is_process_alive_with_birth(123, 100.0) is False


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX zombie semantics")
def test_real_zombie_is_not_alive() -> None:
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(0)

    try:
        child = psutil.Process(child_pid)
        deadline = time.monotonic() + 2.0
        while child.status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() >= deadline:
                pytest.fail("child did not enter zombie state")
            time.sleep(0.01)
        birth = child.create_time()

        assert liveness.is_process_alive(child_pid) is False
        assert liveness.is_process_alive_with_birth(child_pid, birth) is False
    finally:
        os.waitpid(child_pid, 0)
def test_is_process_alive_returns_true_on_access_denied(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    def _raise_access_denied(pid: int):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(liveness.psutil, "Process", _raise_access_denied)

    assert liveness.is_process_alive(123) is True
def test_is_spawn_genuinely_active_uses_runner_pid_liveness(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(status="running", runner_pid=4321)
    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )
    monkeypatch.setattr(liveness, "is_process_alive", lambda pid: pid == 4321)

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is True
def test_is_spawn_genuinely_active_returns_false_for_stale_heartbeat(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(status="running", runner_pid=None)
    heartbeat = tmp_path / "spawns" / "p1" / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()
    old_mtime = time.time() - 121.0
    os.utime(heartbeat, (old_mtime, old_mtime))

    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )
    monkeypatch.setattr(liveness.time, "time", lambda: old_mtime + 121.0)

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is False


def test_is_process_alive_with_birth_returns_true_for_matching_live_process(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=100.0))

    assert liveness.is_process_alive_with_birth(123, 100.5) is True


def test_is_process_alive_with_birth_returns_false_for_birth_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=102.1))

    assert liveness.is_process_alive_with_birth(123, 100.0) is False


def test_is_process_alive_with_birth_fails_closed_for_unknown_birth(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    assert liveness.is_process_alive_with_birth(123, None) is False
    assert liveness.is_process_alive_with_birth(123, 0.0) is False
