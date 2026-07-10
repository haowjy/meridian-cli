"""Detached process parent-death watchdog.

Runs as a tiny helper process on POSIX platforms that lack a native
parent-death signal.  It watches the Meridian launcher process and terminates
an already-isolated target process scope if the launcher disappears before the
scope is empty.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psutil

from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    ProcessScopeSnapshot,
    birth_time_unverified,
)

_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class WatchedProcess:
    pid: int
    created_at_epoch: float


def process_is_same_birth(process: WatchedProcess) -> bool:
    """Return True when *process* is alive and still has the recorded birth time."""

    if process.pid <= 0:
        return False
    try:
        actual = psutil.Process(process.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    if birth_time_unverified(process.created_at_epoch):
        return True
    return abs(actual - process.created_at_epoch) <= 1.0


def process_scope_is_alive(scope: ProcessScopeSnapshot) -> bool:
    """Return True while the scope root or one of its process-group members lives."""

    root = WatchedProcess(
        pid=scope.root_pid,
        created_at_epoch=scope.root_created_at_epoch,
    )
    if process_is_same_birth(root):
        return True
    if scope.pgid is None:
        return False

    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.info["status"] == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(process.pid) == scope.pgid:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return False


def watch_parent_until_exit(
    *,
    parent: WatchedProcess,
    target_scope: ProcessScopeSnapshot,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    grace_seconds: float = _DEFAULT_GRACE_SECONDS,
    parent_alive: Callable[[WatchedProcess], bool] = process_is_same_birth,
    target_alive: Callable[[ProcessScopeSnapshot], bool] = process_scope_is_alive,
    terminate_scope: Callable[..., object] = terminate_scope_sync,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Watch *parent* and terminate *target_scope* if the parent dies first."""

    while True:
        if not target_alive(target_scope):
            return 0
        if not parent_alive(parent):
            terminate_scope(
                target_scope,
                grace_seconds=grace_seconds,
                reason="parent_exit_watchdog",
            )
            return 0
        sleep(max(0.05, poll_interval_seconds))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created-at", type=float, required=True)
    parser.add_argument("--target-pid", type=int, required=True)
    parser.add_argument("--target-created-at", type=float, required=True)
    parser.add_argument("--target-pgid", type=int, default=0)
    parser.add_argument("--scope-id", default="backend")
    parser.add_argument("--ready-fd", type=int, default=-1)
    parser.add_argument("--poll-interval", type=float, default=_DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--grace-seconds", type=float, default=_DEFAULT_GRACE_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    target_pgid = args.target_pgid if args.target_pgid > 0 else None
    target_scope = ProcessScopeSnapshot(
        scope_id=args.scope_id,
        owner_policy="spawn_owned",
        owner_id=f"parent-watchdog:{args.parent_pid}",
        role="harness_backend",
        containment="posix_pgid" if target_pgid is not None else "pid_tree_fallback",
        root_pid=args.target_pid,
        root_created_at_epoch=args.target_created_at or PROCESS_BIRTH_UNKNOWN_EPOCH,
        pgid=target_pgid,
        job_name=None,
        degraded_reason=None,
    )
    if args.ready_fd >= 0:
        try:
            os.write(args.ready_fd, b"1")
        finally:
            os.close(args.ready_fd)
    return watch_parent_until_exit(
        parent=WatchedProcess(
            pid=args.parent_pid,
            created_at_epoch=args.parent_created_at or PROCESS_BIRTH_UNKNOWN_EPOCH,
        ),
        target_scope=target_scope,
        poll_interval_seconds=args.poll_interval,
        grace_seconds=args.grace_seconds,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess smoke tests
    raise SystemExit(main())


__all__ = [
    "WatchedProcess",
    "main",
    "process_is_same_birth",
    "process_scope_is_alive",
    "watch_parent_until_exit",
]
