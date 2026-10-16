"""Platform tests for terminate_managed_primary_processes birth-match signaling."""

from __future__ import annotations

import pytest

from meridian.lib.state.managed_primary import terminate_managed_primary_processes
from meridian.lib.state.primary_meta import PrimaryMetadata


@pytest.mark.parametrize(
    ("metadata", "live_births", "expected"),
    [
        (
            PrimaryMetadata(
                managed_backend=True,
                launcher_pid=9101,
                launcher_birth_epoch=100.0,
                backend_pid=9102,
                backend_birth_epoch=200.0,
                tui_pid=9103,
                tui_birth_epoch=None,
            ),
            {9101: 101.2, 9102: 200.0, 9103: 300.0},
            (9102,),
        ),
        (
            PrimaryMetadata(
                managed_backend=True,
                launcher_pid=9201,
                launcher_birth_epoch=120.0,
                backend_pid=9202,
                backend_birth_epoch=220.0,
                tui_pid=9203,
                tui_birth_epoch=320.0,
            ),
            {9201: 120.0, 9202: 221.1, 9203: 320.0},
            (9201, 9203),
        ),
    ],
)
def test_terminate_managed_primary_processes_signals_only_birth_matches(
    monkeypatch: pytest.MonkeyPatch,
    metadata: PrimaryMetadata,
    live_births: dict[int, float],
    expected: tuple[int, ...],
) -> None:
    terminated_pids: list[int] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return live_births[self.pid]

        def is_running(self) -> bool:
            return True

        def status(self) -> str:
            return "running"

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

    assert signaled == expected
    assert terminated_pids == list(expected)
