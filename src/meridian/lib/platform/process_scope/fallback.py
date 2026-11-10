"""Tree termination: the one place Meridian signals a process and its descendants."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import psutil

from meridian.lib.platform.process_scope.base import (
    POST_KILL_WAIT_SECS,
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    CleanupResult,
    RootIdentity,
    birth_time_unverified,
    check_root_identity,
    process_birth_epoch,
)


def _snapshot_tree(pid: int) -> tuple[psutil.Process | None, list[psutil.Process]]:
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None, []

    children: list[psutil.Process] = []
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        children = root.children(recursive=True)
    return root, children


def terminate_tree_sync(
    pid: int,
    *,
    created_at_epoch: float,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
    degraded_fallback: bool = False,
) -> CleanupResult:
    """Terminate ``pid`` and its descendants if ``created_at_epoch`` verifies it.

    A recorded birth that is unset, unreadable or different means the PID is not
    known to be the process Meridian started; nothing is signalled.
    """

    identity = check_root_identity(pid, created_at_epoch)
    if identity in (RootIdentity.REUSED, RootIdentity.UNKNOWN):
        return CleanupResult.skipped(
            scope_id=scope_id,
            root_pid=pid,
            reason=reason,
            grace_seconds=grace_secs,
            degraded_fallback=degraded_fallback,
            identity=identity,
        )

    root, children = _snapshot_tree(pid) if identity is RootIdentity.VERIFIED else (None, [])

    descendant_count = len(children)
    kill_escalated = False
    if root is not None:
        tree = [*children, root]
        for proc in tree:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.terminate()

        _, alive = psutil.wait_procs(tree, timeout=grace_secs)
        kill_escalated = bool(alive)
        if alive:
            for proc in alive:
                with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    proc.kill()
            psutil.wait_procs(alive, timeout=POST_KILL_WAIT_SECS)

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


async def terminate_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
    root_created_at_epoch: float = PROCESS_BIRTH_UNKNOWN_EPOCH,
    degraded_fallback: bool = False,
) -> CleanupResult:
    """Terminate a child the caller holds. An unreaped child's PID is its own, so
    an unrecorded birth is read from the live process instead of skipping it."""

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
    epoch = (
        process_birth_epoch(pid)
        if birth_time_unverified(root_created_at_epoch)
        else root_created_at_epoch
    )
    return await asyncio.to_thread(
        terminate_tree_sync,
        pid,
        created_at_epoch=epoch,
        grace_secs=grace_secs,
        reason=reason,
        scope_id=scope_id,
        degraded_fallback=degraded_fallback,
    )


__all__ = ["terminate_tree", "terminate_tree_sync"]
