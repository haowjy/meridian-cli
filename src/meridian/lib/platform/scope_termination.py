"""Dependency-free synchronous process-scope termination primitives.

This module is shared by Meridian's process-scope API and the standalone parent
watchdog. Keep its imports limited to the standard library and ``psutil`` so the
watchdog does not load the Meridian package graph.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Sequence
from contextlib import suppress

import psutil

PROCESS_BIRTH_UNKNOWN_EPOCH = 0.0
IS_WINDOWS = os.name == "nt"


class ProcessScope:
    """Fields required to terminate a persisted process scope."""

    __slots__ = ()

    scope_id: str
    containment: str
    root_pid: int
    root_created_at_epoch: float
    pgid: int | None


class TerminationOutcome:
    """Dependency-free facts returned by a synchronous termination attempt."""

    __slots__ = (
        "degraded_fallback",
        "descendant_count",
        "kill_escalated",
        "skip_reason",
    )

    def __init__(
        self,
        *,
        descendant_count: int | None,
        kill_escalated: bool,
        degraded_fallback: bool,
        skip_reason: str | None,
    ) -> None:
        self.descendant_count = descendant_count
        self.kill_escalated = kill_escalated
        self.degraded_fallback = degraded_fallback
        self.skip_reason = skip_reason


def birth_time_unverified(created_at_epoch: float) -> bool:
    """Return True when process birth time was not observed."""

    return created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH


def validate_pid_birth(root_pid: int, created_at_epoch: float) -> str | None:
    """Return a skip reason if *root_pid* has been reused."""

    if birth_time_unverified(created_at_epoch):
        return None

    with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        actual = psutil.Process(root_pid).create_time()
        if abs(actual - created_at_epoch) > 1.0:
            return "pid_reuse_detected"

    return None


def snapshot_process_tree(
    pid: int,
) -> tuple[psutil.Process | None, list[psutil.Process]]:
    """Return a stable snapshot of a root process and its descendants."""

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
    created_at_epoch: float = PROCESS_BIRTH_UNKNOWN_EPOCH,
    grace_secs: float = 5.0,
    degraded_fallback: bool = False,
) -> TerminationOutcome:
    """Terminate a process tree, escalating to SIGKILL after *grace_secs*."""

    skip_reason = validate_pid_birth(pid, created_at_epoch)
    if skip_reason is not None:
        return TerminationOutcome(
            descendant_count=None,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=skip_reason,
        )

    root, children = snapshot_process_tree(pid)
    if root is None:
        return TerminationOutcome(
            descendant_count=0,
            kill_escalated=False,
            degraded_fallback=degraded_fallback,
            skip_reason=None,
        )

    tree = [*children, root]
    for process in tree:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.terminate()

    _, alive = psutil.wait_procs(tree, timeout=grace_secs)
    kill_escalated = bool(alive)
    if alive:
        for process in alive:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.kill()
        psutil.wait_procs(alive, timeout=1.0)

    return TerminationOutcome(
        descendant_count=len(children),
        kill_escalated=kill_escalated,
        degraded_fallback=degraded_fallback,
        skip_reason=None,
    )


def is_pgid_reachable(pgid: int) -> bool:
    """Return whether signal 0 can reach a POSIX process group."""

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


def _scan_process_group(pgid: int) -> list[psutil.Process]:
    """Return all live processes whose process group ID matches *pgid*."""

    result: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            if os.getpgid(process.pid) == pgid:
                result.append(process)
    return result


def terminate_pgid(
    pgid: int,
    root_pid: int,
    created_at_epoch: float,
    grace_seconds: float,
) -> TerminationOutcome:
    """Terminate a POSIX process group with PID-reuse protection."""

    if IS_WINDOWS:
        raise RuntimeError("terminate_pgid() is not available on Windows.")

    if pgid != root_pid:
        return terminate_tree_sync(
            pid=root_pid,
            created_at_epoch=created_at_epoch,
            grace_secs=grace_seconds,
            degraded_fallback=True,
        )

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
        return TerminationOutcome(
            descendant_count=None,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason="pid_reuse_detected",
        )

    root_process: psutil.Process | None = None
    children: list[psutil.Process] = []
    try:
        root_process = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        root_is_dead = True
    except psutil.AccessDenied:
        pass
    if root_process is not None:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            children = root_process.children(recursive=True)

    orphans = _scan_process_group(pgid) if root_is_dead else []

    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)

    tree: Sequence[psutil.Process] = (
        orphans if root_is_dead else ([root_process] if root_process is not None else []) + children
    )
    kill_escalated = False
    if tree:
        _, alive = psutil.wait_procs(tree, timeout=grace_seconds)
        if alive:
            kill_escalated = True
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            for process in alive:
                with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    process.kill()
            psutil.wait_procs(alive, timeout=1.0)

    return TerminationOutcome(
        descendant_count=len(tree) if root_is_dead else len(children),
        kill_escalated=kill_escalated,
        degraded_fallback=root_is_dead,
        skip_reason=None,
    )


def terminate_scope_sync(
    scope: ProcessScope,
    *,
    grace_seconds: float,
) -> TerminationOutcome:
    """Synchronously terminate *scope* through its containment mechanism."""

    if scope.containment == "posix_pgid" and scope.pgid is not None:
        return terminate_pgid(
            pgid=scope.pgid,
            root_pid=scope.root_pid,
            created_at_epoch=scope.root_created_at_epoch,
            grace_seconds=grace_seconds,
        )
    return terminate_tree_sync(
        pid=scope.root_pid,
        created_at_epoch=scope.root_created_at_epoch,
        grace_secs=grace_seconds,
        degraded_fallback=scope.containment != "pid_tree_fallback",
    )


__all__ = [
    "IS_WINDOWS",
    "PROCESS_BIRTH_UNKNOWN_EPOCH",
    "ProcessScope",
    "TerminationOutcome",
    "birth_time_unverified",
    "is_pgid_reachable",
    "snapshot_process_tree",
    "terminate_pgid",
    "terminate_scope_sync",
    "terminate_tree_sync",
    "validate_pid_birth",
]
