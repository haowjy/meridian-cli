from __future__ import annotations

from dataclasses import dataclass

import psutil
import pytest

from meridian.lib.platform.process_scope import fallback
from meridian.lib.platform.process_scope.base import CleanupResult


class _FakeAsyncProcess:
    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode


@dataclass
class _FakeProc:
    pid: int
    terminate_exc: BaseException | None = None
    kill_exc: BaseException | None = None
    terminated: int = 0
    killed: int = 0
    create_time_value: float = 100.0
    _children: list[object] | None = None

    def create_time(self) -> float:
        return self.create_time_value

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return list(self._children or [])

    def terminate(self) -> None:
        self.terminated += 1
        if self.terminate_exc is not None:
            raise self.terminate_exc

    def kill(self) -> None:
        self.killed += 1
        if self.kill_exc is not None:
            raise self.kill_exc


@pytest.mark.asyncio
async def test_terminate_tree_short_circuits_for_exited_process() -> None:
    process = _FakeAsyncProcess(pid=111, returncode=0)

    result = await fallback.terminate_tree(
        process,
        grace_secs=9.0,
        reason="stop_called",
        scope_id="backend",
        degraded_fallback=True,
    )

    assert result == CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=0,
        reason="stop_called",
        grace_seconds=9.0,
        kill_escalated=False,
        degraded_fallback=True,
        skip_reason=None,
    )


def test_terminate_tree_sync_skips_on_pid_reuse_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _FakeProc(pid=111, create_time_value=100.0)
    monkeypatch.setattr(psutil, "Process", lambda _pid: root)

    result = fallback.terminate_tree_sync(
        111,
        created_at_epoch=999.0,
        grace_secs=1.0,
        reason="reaper",
        scope_id="backend",
    )

    assert result.skip_reason == "pid_reuse_detected"
    assert result.kill_escalated is False
    assert root.terminated == 0
    assert root.killed == 0


def test_terminate_tree_sync_escalates_and_suppresses_racy_process_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _FakeProc(pid=112, terminate_exc=psutil.NoSuchProcess(112))
    root = _FakeProc(pid=111, _children=[child])
    monkeypatch.setattr(psutil, "Process", lambda _pid: root)

    wait_calls: list[list[int]] = []

    def _fake_wait_procs(procs: list[object], timeout: float):
        wait_calls.append([proc.pid for proc in procs])
        if len(wait_calls) == 1:
            return [], list(procs)
        return [], []

    monkeypatch.setattr(psutil, "wait_procs", _fake_wait_procs)

    result = fallback.terminate_tree_sync(
        111,
        created_at_epoch=100.0,
        grace_secs=0.5,
        reason="reaper",
        scope_id="backend",
    )

    assert result == CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=1,
        reason="reaper",
        grace_seconds=0.5,
        kill_escalated=True,
        degraded_fallback=False,
        skip_reason=None,
    )
    assert root.terminated == 1
    assert root.killed == 1
    assert child.terminated == 1
    assert child.killed == 1
    assert wait_calls == [[112, 111], [112, 111]]
