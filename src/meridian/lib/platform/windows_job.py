"""Windows Job Object assignment helper.

Importable on POSIX — all Windows-specific code is guarded by IS_WINDOWS.
Uses ctypes only; no pywin32 dependency.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from meridian.lib.platform import IS_WINDOWS

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def assign_to_new_job(pid: int) -> tuple[str, Any] | None:
    """Create a Job Object and assign *pid* to it.

    Returns ``(job_name, job_handle)`` on success, or ``None`` if not on
    Windows or if assignment fails.  The caller must keep the handle alive;
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` terminates all descendants when
    the handle is closed or the process that holds it exits.
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

    # Configure KILL_ON_JOB_CLOSE so all processes die when handle is released.
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


__all__ = ["assign_to_new_job"]
