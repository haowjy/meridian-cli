"""Cross-platform psutil tree fallback terminator.

Absorbs terminate_tree() logic from platform/terminate.py, extended to return
CleanupResult.  Works on both POSIX and Windows via psutil.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import psutil

from meridian.lib.platform import scope_termination
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    CleanupResult,
)

_UNSET_EPOCH = PROCESS_BIRTH_UNKNOWN_EPOCH


async def terminate_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
    root_created_at_epoch: float = _UNSET_EPOCH,
    degraded_fallback: bool = False,
) -> CleanupResult:
    """Terminate a subprocess tree, escalating to SIGKILL after grace_secs.

    Validates PID birth time before sending signals (PROC-006).
    Extended from the original platform/terminate.py to return CleanupResult.
    """

    pid = process.pid

    if process.returncode is not None:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    skip_reason = scope_termination.validate_pid_birth(pid, root_created_at_epoch)
    if skip_reason is not None:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=skip_reason,
        )

    root, children = scope_termination.snapshot_process_tree(pid)
    if root is None:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=pid,
            descendant_count=0,
            reason=reason,
            grace_seconds=grace_secs,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    descendant_count = len(children)
    tree = [*children, root]

    for proc in tree:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()

    _, alive = await asyncio.to_thread(psutil.wait_procs, tree, timeout=grace_secs)
    kill_escalated = bool(alive)
    if alive:
        for proc in alive:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        await asyncio.to_thread(psutil.wait_procs, alive, timeout=1.0)

    return CleanupResult(
        scope_id=scope_id,
        root_pid=pid,
        descendant_count=descendant_count,
        reason=reason,
        grace_seconds=grace_secs,
        kill_escalated=kill_escalated,
        degraded_fallback=degraded_fallback,
        skip_reason=None,
    )


def terminate_tree_sync(
    pid: int,
    *,
    created_at_epoch: float = _UNSET_EPOCH,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
    degraded_fallback: bool = False,
) -> CleanupResult:
    """Synchronous variant for hook contexts (no event loop required)."""

    outcome = scope_termination.terminate_tree_sync(
        pid,
        created_at_epoch=created_at_epoch,
        grace_secs=grace_secs,
        degraded_fallback=degraded_fallback,
    )
    return CleanupResult(
        scope_id=scope_id,
        root_pid=pid,
        descendant_count=outcome.descendant_count,
        reason=reason,
        grace_seconds=grace_secs,
        kill_escalated=outcome.kill_escalated,
        degraded_fallback=outcome.degraded_fallback,
        skip_reason=outcome.skip_reason,
    )


__all__ = ["PROCESS_BIRTH_UNKNOWN_EPOCH", "terminate_tree", "terminate_tree_sync"]
