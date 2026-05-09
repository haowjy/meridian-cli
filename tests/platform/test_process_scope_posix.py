from __future__ import annotations

import os
import signal
from dataclasses import dataclass

import psutil
import pytest

from meridian.lib.platform.process_scope import posix
from meridian.lib.platform.process_scope.base import CleanupResult


@dataclass
class _FakeProc:
    pid: int
    _children: list[object]
    killed: int = 0

    def create_time(self) -> float:
        return 100.0

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return list(self._children)

    def kill(self) -> None:
        self.killed += 1


class _FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def test_terminate_pgid_skips_when_birth_time_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(posix, "IS_WINDOWS", False)
    monkeypatch.setattr(psutil, "Process", lambda _pid: _FakeProc(_pid, []))

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    result = posix.terminate_pgid(
        pgid=222,
        root_pid=111,
        created_at_epoch=999.0,
        grace_seconds=5.0,
        reason="reaper",
        scope_id="backend",
    )

    assert result == CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=None,
        reason="reaper",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason="pid_reuse_detected",
    )
    assert killpg_calls == []


def test_terminate_pgid_on_windows_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(posix, "IS_WINDOWS", True)

    with pytest.raises(RuntimeError, match="not available on Windows"):
        posix.terminate_pgid(
            pgid=222,
            root_pid=111,
            created_at_epoch=100.0,
            grace_seconds=5.0,
            reason="reaper",
            scope_id="backend",
        )


def test_terminate_pgid_escalates_to_sigkill_for_remaining_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(posix, "IS_WINDOWS", False)
    root = _FakeProc(111, [_FakeProc(112, []), _FakeProc(113, [])])
    monkeypatch.setattr(psutil, "Process", lambda _pid: root)

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))

    wait_calls: list[list[int]] = []

    def _fake_wait_procs(procs: list[object], timeout: float):
        wait_calls.append([proc.pid for proc in procs])
        if len(wait_calls) == 1:
            return [], list(procs)
        return [], []

    monkeypatch.setattr(psutil, "wait_procs", _fake_wait_procs)

    result = posix.terminate_pgid(
        pgid=222,
        root_pid=111,
        created_at_epoch=100.0,
        grace_seconds=0.25,
        reason="stop_called",
        scope_id="backend",
    )

    assert result == CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=2,
        reason="stop_called",
        grace_seconds=0.25,
        kill_escalated=True,
        degraded_fallback=False,
        skip_reason=None,
    )
    assert signals == [(222, signal.SIGTERM), (222, signal.SIGKILL)]
    assert wait_calls == [[111, 112, 113], [111, 112, 113]]
