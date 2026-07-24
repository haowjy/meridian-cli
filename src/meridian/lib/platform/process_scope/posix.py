"""POSIX process-group scope adapter.

Importable on Windows, but process-group operations are POSIX-only.
"""

from __future__ import annotations

import os
from contextlib import suppress

import psutil

from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import CleanupResult, birth_time_unverified


def is_pgid_reachable(pgid: int) -> bool:
    """Return whether signal 0 can reach a POSIX process group.

    This is deliberately weaker than an "alive" check: process-group IDs have
    no birth-time reuse guard, and signal 0 also succeeds for groups containing
    only zombies. Use this as best-effort diagnostics, never process identity.
    """

    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _scan_by_pgid(pgid: int) -> list[psutil.Process]:
    """Return all live processes whose process group ID matches *pgid*.

    Full-process-table scan — used only as a degraded fallback when the
    root process is already dead and the normal PGID-kill path produced a
    ProcessLookupError (group dissolved before SIGTERM landed).
    """
    result: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid"]):
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            if os.getpgid(proc.pid) == pgid:
                result.append(proc)
    return result


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

    When the root process is already dead (``NoSuchProcess`` during birth-time
    check), the function still attempts PGID termination in degraded mode
    (PROC-004).  If the process group has also dissolved (``ProcessLookupError``
    from ``os.killpg``), a secondary full-table scan by PGID is performed to
    catch orphaned descendants that re-parented but stayed in the group.

    Returns a CleanupResult — never raises.
    """

    if IS_WINDOWS:
        raise RuntimeError("terminate_pgid() is not available on Windows.")

    if pgid != root_pid:
        from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

        return terminate_tree_sync(
            pid=root_pid,
            created_at_epoch=created_at_epoch,
            grace_secs=grace_seconds,
            reason=reason,
            scope_id=scope_id,
            degraded_fallback=True,
        )

    import signal

    # --- PID reuse guard (PROC-006) --- fail-closed for confirmed reuse only.
    # Distinguishes "PID reused by another process" (hard skip) from "root is
    # already dead" (degraded mode — PGID kill may still reach live members).
    pid_reuse_detected = False
    root_is_dead = False
    if not birth_time_unverified(created_at_epoch):
        try:
            actual_create_time = psutil.Process(root_pid).create_time()
            if abs(actual_create_time - created_at_epoch) > 1.0:
                pid_reuse_detected = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            root_is_dead = True

    if pid_reuse_detected:
        return CleanupResult(
            scope_id=scope_id,
            root_pid=root_pid,
            descendant_count=None,
            reason=reason,
            grace_seconds=grace_seconds,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason="pid_reuse_detected",
        )

    # --- Snapshot tree before signalling for wait + descendant count ---
    root_proc: psutil.Process | None = None
    children: list[psutil.Process] = []
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        root_proc = psutil.Process(root_pid)
    if root_proc is not None:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            children = root_proc.children(recursive=True)

    # --- SIGTERM to the process group ---
    # Even when root_is_dead the group may still have live members — attempt it.
    pgid_group_dissolved = False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pgid_group_dissolved = True
    except (PermissionError, OSError):
        pass

    # --- Build the process tree to wait on ---
    tree: list[psutil.Process] = ([root_proc] if root_proc is not None else []) + children

    # --- Secondary sweep (PROC-004): dead root + dissolved group ---
    # When the root died before snapshot AND the group has dissolved, scan all
    # processes for any remaining PGID members (orphaned, re-parented to init).
    orphan_scan_performed = False
    if root_is_dead and pgid_group_dissolved:
        orphans = _scan_by_pgid(pgid)
        if orphans:
            tree = orphans
            orphan_scan_performed = True

    # Determine descendant count for the result record.
    descendant_count: int | None
    if root_is_dead:
        # When root was dead: orphan-scan count if we found any, else unknown.
        descendant_count = len(tree) if orphan_scan_performed else None
    else:
        descendant_count = len(children)

    # degraded when root was already dead before we started
    degraded_fallback = root_is_dead

    kill_escalated = False
    if tree:
        _, alive = psutil.wait_procs(tree, timeout=grace_seconds)
        if alive:
            kill_escalated = True
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            for proc in alive:
                with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    proc.kill()
            psutil.wait_procs(alive, timeout=1.0)

    return CleanupResult(
        scope_id=scope_id,
        root_pid=root_pid,
        descendant_count=descendant_count,
        reason=reason,
        grace_seconds=grace_seconds,
        kill_escalated=kill_escalated,
        degraded_fallback=degraded_fallback,
        skip_reason=None,
    )


__all__ = ["is_pgid_reachable", "terminate_pgid"]
