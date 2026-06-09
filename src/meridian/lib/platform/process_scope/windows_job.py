"""Windows Job Object scope adapter.

Importable on POSIX, but functions operate only on Windows.
Uses ctypes — not pywin32.  Targets Python >= 3.12 with nested-job support.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from typing import Any, cast

import psutil

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import CleanupResult, birth_time_unverified

# ---------------------------------------------------------------------------
# Windows constants (defined inline so the module imports cleanly on POSIX)
# ---------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def assign_to_new_job(pid: int) -> tuple[str, Any] | None:
    """Create a Job Object and assign *pid* to it.

    Returns ``(job_name, job_handle)`` on success, or ``None`` if not on
    Windows or if assignment fails.  The handle must be kept alive for
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` to take effect.
    """

    if not IS_WINDOWS:
        return None

    import ctypes
    import ctypes.wintypes

    kernel32 = cast("Any", ctypes.windll.kernel32)  # type: ignore[attr-defined]

    job_name = f"meridian-scope-{uuid.uuid4().hex}"
    job_handle = kernel32.CreateJobObjectW(None, job_name)
    if not job_handle:
        return None

    # Set KILL_ON_JOB_CLOSE so descendants die when we close the handle.
    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", ctypes.wintypes.DWORD),
            ("SchedulingClass", ctypes.wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    ok = kernel32.SetInformationJobObject(
        job_handle,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job_handle)
        return None

    # Open a handle to the target process.
    PROCESS_ALL_ACCESS = 0x1F0FFF
    proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not proc_handle:
        kernel32.CloseHandle(job_handle)
        return None

    assigned = kernel32.AssignProcessToJobObject(job_handle, proc_handle)
    kernel32.CloseHandle(proc_handle)

    if not assigned:
        kernel32.CloseHandle(job_handle)
        return None

    return job_name, job_handle


def terminate_job(
    job_handle: Any,
    root_pid: int,
    created_at_epoch: float,
    grace_seconds: float,
    reason: str,
    scope_id: str,
) -> CleanupResult:
    """Terminate all processes in a Windows Job Object.

    Validates PID birth time before signalling (PROC-006).
    Returns CleanupResult with ``degraded_fallback=True`` if *job_handle* is
    None or if not running on Windows (handle was never created).
    """

    if not IS_WINDOWS or job_handle is None:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=root_pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=True,
            skip_reason="job_assignment_failed" if job_handle is None else "not_windows",
        )

    import ctypes

    kernel32 = cast("Any", ctypes.windll.kernel32)  # type: ignore[attr-defined]

    # --- PID reuse guard (PROC-006) ---
    skip_reason: str | None = None
    if not birth_time_unverified(created_at_epoch):
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            actual_create_time = psutil.Process(root_pid).create_time()
            if abs(actual_create_time - created_at_epoch) > 1.0:
                skip_reason = "pid_reuse_detected"

    if skip_reason is not None:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=root_pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=skip_reason,
        )

    descendant_count: int | None = None
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        descendant_count = len(psutil.Process(root_pid).children(recursive=True))

    # TerminateJobObject exits all processes with the given exit code.
    kernel32.TerminateJobObject(job_handle, 1)
    kernel32.CloseHandle(job_handle)

    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=descendant_count,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=False,
        degraded_fallback=False,
        skip_reason=None,
    )


__all__ = ["assign_to_new_job", "terminate_job"]
