"""POSIX process-group scope adapter.

Importable on Windows, but all functions raise RuntimeError if called there.
"""

from __future__ import annotations

from contextlib import suppress

import psutil

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import CleanupResult


def terminate_pgid(
    pgid: int,
    root_pid: int,
    created_at_epoch: float,
    grace_seconds: float,
    reason: str,
    scope_id: str,
) -> CleanupResult:
    """Send SIGTERM to the POSIX process group, escalating to SIGKILL if needed.

    Validates the root PID birth time before sending signals (PROC-006).
    Returns a CleanupResult — never raises.
    """

    if IS_WINDOWS:
        raise RuntimeError("terminate_pgid() is not available on Windows.")

    import os
    import signal

    # --- PID reuse guard (PROC-006) ---
    skip_reason: str | None = None
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

    # --- Snapshot tree before signalling for wait + descendant count ---
    root_proc: psutil.Process | None = None
    children: list[psutil.Process] = []
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        root_proc = psutil.Process(root_pid)
    if root_proc is not None:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            children = root_proc.children(recursive=True)

    descendant_count = len(children)
    tree = ([root_proc] if root_proc is not None else []) + children

    # --- SIGTERM to the process group ---
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)

    kill_escalated = False

    if tree:
        _, alive = psutil.wait_procs(tree, timeout=grace_seconds)
        if alive:
            kill_escalated = True
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            psutil.wait_procs(alive, timeout=1.0)

    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=descendant_count,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=kill_escalated,
        degraded_fallback=False,
        skip_reason=None,
    )


__all__ = ["terminate_pgid"]
