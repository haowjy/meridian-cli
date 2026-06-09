"""Unit tests for the Windows Job Object process-scope terminator."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil

from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH


def test_unknown_birth_time_proceeds_to_terminate_owned_job(monkeypatch) -> None:
    """Unknown birth time means unverified, not reused: owned teardown proceeds."""
    from meridian.lib.platform.process_scope import windows_job

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
