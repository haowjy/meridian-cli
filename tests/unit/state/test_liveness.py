from __future__ import annotations

import os
import time
from types import SimpleNamespace

import psutil

from meridian.lib.state import liveness


class _FakeProcess:
    def __init__(self, *, create_time: float = 100.0, is_running: bool = True) -> None:
        self._create_time = create_time
        self._is_running = is_running

    def create_time(self) -> float:
        return self._create_time

    def is_running(self) -> bool:
        return self._is_running


def test_is_process_alive_returns_false_when_pid_does_not_exist(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: False)

    assert liveness.is_process_alive(123) is False


def test_is_process_alive_returns_false_for_pid_reuse(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=131.0))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is False


def test_is_process_alive_allows_expected_process_startup_delay(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(create_time=120.0))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is True


def test_is_process_alive_returns_process_running_state(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(liveness.psutil, "Process", lambda pid: _FakeProcess(is_running=True))

    assert liveness.is_process_alive(123, created_after_epoch=100.0) is True


def test_is_process_alive_returns_false_when_process_disappears(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    def _raise_no_such_process(pid: int):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(liveness.psutil, "Process", _raise_no_such_process)

    assert liveness.is_process_alive(123) is False


def test_is_process_alive_returns_true_on_access_denied(monkeypatch) -> None:
    monkeypatch.setattr(liveness.psutil, "pid_exists", lambda pid: True)

    def _raise_access_denied(pid: int):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(liveness.psutil, "Process", _raise_access_denied)

    assert liveness.is_process_alive(123) is True


def test_is_spawn_genuinely_active_returns_false_when_record_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: None)

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is False


def test_is_spawn_genuinely_active_returns_false_for_terminal_status(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(status="failed", runner_pid=os.getpid())
    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is False


def test_is_spawn_genuinely_active_uses_runner_pid_liveness(tmp_path, monkeypatch) -> None:
    record = SimpleNamespace(status="running", runner_pid=4321)
    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )
    monkeypatch.setattr(liveness, "is_process_alive", lambda pid: pid == 4321)

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is True


def test_is_spawn_genuinely_active_uses_fresh_heartbeat_when_runner_pid_is_missing(
    tmp_path, monkeypatch
) -> None:
    record = SimpleNamespace(status="running", runner_pid=None)
    heartbeat = tmp_path / "spawns" / "p1" / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()

    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )
    monkeypatch.setattr(liveness.time, "time", lambda: heartbeat.stat().st_mtime + 60.0)

    assert liveness.is_spawn_genuinely_active(tmp_path, "p1") is True


def test_is_spawn_genuinely_active_uses_fresh_heartbeat_when_runner_pid_is_dead(
    tmp_path, monkeypatch
) -> None:
    record = SimpleNamespace(status="running", runner_pid=4321)
    heartbeat = tmp_path / "spawns" / "p1" / "heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()

    monkeypatch.setattr("meridian.lib.state.spawn_store.get_spawn", lambda *_args: record)
    monkeypatch.setattr(
        "meridian.lib.core.spawn_lifecycle.is_active_spawn_status",
        lambda status: status in {"queued", "running", "finalizing"},
    )
    monkeypatch.setattr(liveness, "is_process_alive", lambda pid: False)
    monkeypatch.setattr(liveness.time, "time", lambda: heartbeat.stat().st_mtime + 60.0)

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
