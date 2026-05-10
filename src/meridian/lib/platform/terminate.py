"""Cross-platform subprocess tree termination helpers."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import psutil

_UNSET_EPOCH = 0.0


def _validate_pid_birth(pid: int, created_at_epoch: float) -> bool:
    """Return False if PID has been reused (birth time mismatch), else True.

    Skips the check when created_at_epoch is 0.0 (sentinel for 'unknown').
    """
    if created_at_epoch == _UNSET_EPOCH:
        return True
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        actual = psutil.Process(pid).create_time()
        if abs(actual - created_at_epoch) > 1.0:
            return False
    return True


def _snapshot_tree(pid: int) -> tuple[psutil.Process | None, list[psutil.Process]]:
    """Return a stable snapshot of root process and descendants."""

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None, []

    children: list[psutil.Process] = []
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        children = root.children(recursive=True)
    return root, children


async def terminate_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_secs: float = 5.0,
) -> None:
    """Terminate a subprocess tree, escalating to kill after a grace period."""

    if process.returncode is not None:
        return

    pid = process.pid
    root, children = _snapshot_tree(pid)
    if root is None:
        return

    tree = [*children, root]

    for proc in tree:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()

    _, alive = await asyncio.to_thread(psutil.wait_procs, tree, timeout=grace_secs)
    if not alive:
        return

    for proc in alive:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()

    await asyncio.to_thread(psutil.wait_procs, alive, timeout=1.0)


def terminate_tree_sync(
    pid: int,
    *,
    created_at_epoch: float = _UNSET_EPOCH,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
) -> None:
    """Synchronous process-tree termination for contexts without an event loop.

    Sends SIGTERM/terminate to the entire process tree rooted at *pid*, waits
    up to *grace_secs* for clean exit, then escalates to SIGKILL/kill for any
    survivors.  Validates PID birth time when *created_at_epoch* is non-zero to
    guard against PID reuse.

    Uses psutil for cross-platform tree discovery and signal delivery (POSIX and
    Windows compatible).  NoSuchProcess / AccessDenied errors are suppressed so
    the function is safe to call on already-dead processes.
    """
    _ = reason, scope_id  # unused here; present for forward-compat with richer variants

    if not _validate_pid_birth(pid, created_at_epoch):
        return

    root, children = _snapshot_tree(pid)
    if root is None:
        return

    tree = [*children, root]

    for proc in tree:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()

    _, alive = psutil.wait_procs(tree, timeout=grace_secs)
    if alive:
        for proc in alive:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        psutil.wait_procs(alive, timeout=1.0)


__all__ = ["terminate_tree", "terminate_tree_sync"]
