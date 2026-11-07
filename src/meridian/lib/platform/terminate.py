from __future__ import annotations

import asyncio
from contextlib import suppress

import psutil

from meridian.lib.platform.process_scope.base import POST_KILL_WAIT_SECS
from meridian.lib.platform.process_scope.fallback import terminate_tree as _terminate_tree

_UNSET_EPOCH = 0.0


def _validate_pid_birth(pid: int, created_at_epoch: float) -> bool:
    if created_at_epoch == _UNSET_EPOCH:
        return True
    # An unavailable birth time does not establish process identity.
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        actual = psutil.Process(pid).create_time()
        if abs(actual - created_at_epoch) > 1.0:
            return False
    return True


def _snapshot_tree(pid: int) -> tuple[psutil.Process | None, list[psutil.Process]]:

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
    await _terminate_tree(process, grace_secs=grace_secs)


def terminate_tree_sync(
    pid: int,
    *,
    created_at_epoch: float = _UNSET_EPOCH,
    grace_secs: float = 5.0,
    reason: str = "stop_called",
    scope_id: str = "",
) -> None:
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
        psutil.wait_procs(alive, timeout=POST_KILL_WAIT_SECS)


__all__ = ["terminate_tree", "terminate_tree_sync"]
