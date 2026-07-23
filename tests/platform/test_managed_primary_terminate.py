"""Platform tests for terminate_managed_primary_processes birth-match signaling."""

from __future__ import annotations

from meridian.lib.state.managed_primary import terminate_managed_primary_processes
from meridian.lib.state.primary_meta import PrimaryMetadata


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
