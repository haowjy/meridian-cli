"""Unit tests for the Windows Job Object process-scope terminator."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest  # noqa: TC002

from meridian.lib.platform.process_scope import windows_job
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH, CleanupResult


class _FakeKernel32:
    def __init__(self) -> None:
        self.close_calls: list[object] = []
        self.terminate_calls: list[tuple[object, int]] = []
        self.create_handle = 101
        self.proc_handle = 202
        self.assign_result = 1
        self.set_info_result = 1

    def CreateJobObjectW(self, *_args: object) -> int:
        return self.create_handle

    def SetInformationJobObject(self, *_args: object) -> int:
        return self.set_info_result

    def OpenProcess(self, *_args: object) -> int:
        return self.proc_handle

    def AssignProcessToJobObject(self, *_args: object) -> int:
        return self.assign_result

    def CloseHandle(self, handle: object) -> int:
        self.close_calls.append(handle)
        return 1

    def TerminateJobObject(self, handle: object, exit_code: int) -> int:
        self.terminate_calls.append((handle, exit_code))
        return 1


class _FakeProc:
    def __init__(self, children: list[object] | None = None, create_time: float = 100.0) -> None:
        self._children = list(children or [])
        self._create_time = create_time

    def create_time(self) -> float:
        return self._create_time

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return list(self._children)


class _FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _patch_windows_kernel32(
    monkeypatch: pytest.MonkeyPatch,
    kernel32: _FakeKernel32,
) -> None:
    monkeypatch.setattr(windows_job, "IS_WINDOWS", True)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)


def test_unknown_birth_time_proceeds_to_terminate_owned_job(monkeypatch) -> None:
    """Unknown birth time means unverified, not reused: owned teardown proceeds."""

    kernel32 = SimpleNamespace(
        TerminateJobObject=MagicMock(return_value=True),
        CloseHandle=MagicMock(return_value=True),
    )
    root_proc = MagicMock(spec=psutil.Process)
    root_proc.create_time.side_effect = AssertionError("birth guard should be skipped")
    root_proc.children.return_value = []

    monkeypatch.setattr(windows_job, "IS_WINDOWS", True)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(windows_job.psutil, "Process", MagicMock(return_value=root_proc))

    job_handle = object()
    result = windows_job.terminate_job(
        job_handle=job_handle,
        root_pid=12345,
        created_at_epoch=PROCESS_BIRTH_UNKNOWN_EPOCH,
        grace_seconds=0.1,
        reason="test_stop",
        scope_id="backend",
    )

    assert result.skip_reason is None
    assert result.degraded_fallback is False
    kernel32.TerminateJobObject.assert_called_once_with(job_handle, 1)
    kernel32.CloseHandle.assert_called_once_with(job_handle)


def test_assign_to_new_job_returns_name_and_handle_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32()
    _patch_windows_kernel32(monkeypatch, kernel32)

    result = windows_job.assign_to_new_job(4242)

    assert result is not None
    job_name, job_handle = result
    assert job_name.startswith("meridian-scope-")
    assert job_handle == 101
    assert kernel32.close_calls == [202]


def test_assign_to_new_job_closes_handles_on_assignment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32()
    kernel32.assign_result = 0
    _patch_windows_kernel32(monkeypatch, kernel32)

    result = windows_job.assign_to_new_job(4242)

    assert result is None
    assert kernel32.close_calls == [202, 101]


def test_terminate_job_returns_degraded_result_when_job_handle_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_job, "IS_WINDOWS", True)

    result = windows_job.terminate_job(
        None,
        root_pid=111,
        created_at_epoch=100.0,
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
        degraded_fallback=True,
        skip_reason="job_assignment_failed",
    )


def test_terminate_job_skips_on_pid_reuse_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32()
    _patch_windows_kernel32(monkeypatch, kernel32)
    monkeypatch.setattr(psutil, "Process", lambda _pid: _FakeProc(create_time=100.0))

    result = windows_job.terminate_job(
        101,
        root_pid=111,
        created_at_epoch=999.0,
        grace_seconds=5.0,
        reason="reaper",
        scope_id="backend",
    )

    assert result.skip_reason == "pid_reuse_detected"
    assert kernel32.terminate_calls == []
    assert kernel32.close_calls == []


def test_terminate_job_calls_terminate_and_close_handle_when_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32()
    _patch_windows_kernel32(monkeypatch, kernel32)
    monkeypatch.setattr(psutil, "Process", lambda _pid: _FakeProc(children=[_FakeChild(112)]))

    result = windows_job.terminate_job(
        101,
        root_pid=111,
        created_at_epoch=100.0,
        grace_seconds=5.0,
        reason="cancel",
        scope_id="backend",
    )

    assert result == CleanupResult(
        scope_id="backend",
        root_pid=111,
        descendant_count=1,
        reason="cancel",
        grace_seconds=5.0,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )
    assert kernel32.terminate_calls == [(101, 1)]
    assert kernel32.close_calls == [101]
