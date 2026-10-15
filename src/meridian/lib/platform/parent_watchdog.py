"""Launcher parent-death watchdog for one or more process scopes.

The watchdog is started before a launcher creates children. The launcher then
registers each concrete scope over a pipe. A graceful launcher shutdown sends
an explicit shutdown message; EOF without that message means the launcher died
and every registered scope is terminated.
"""

from __future__ import annotations

import argparse
import os
import select
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psutil

from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot, birth_time_unverified
from meridian.lib.utils.json_shapes import json_object, loads_json_object

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
        actual_process = psutil.Process(process.pid)
        if actual_process.status() == psutil.STATUS_ZOMBIE:
            return False
        actual = actual_process.create_time()
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
    """Watch one scope. Kept as the small pure seam used by scope tests."""

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


def _scope_from_wire(payload: object) -> ProcessScopeSnapshot:
    narrowed = json_object(payload)
    if narrowed is None:
        raise ValueError("watchdog scope payload must be an object")
    payload = narrowed
    return ProcessScopeSnapshot(
        scope_id=str(payload["scope_id"]),
        owner_policy=str(payload["owner_policy"]),
        owner_id=str(payload["owner_id"]),
        role=str(payload["role"]),
        containment=str(payload["containment"]),
        root_pid=int(str(payload["root_pid"])),
        root_created_at_epoch=float(str(payload["root_created_at_epoch"])),
        pgid=int(str(payload["pgid"])) if payload.get("pgid") is not None else None,
        job_name=str(payload["job_name"]) if payload.get("job_name") is not None else None,
        degraded_reason=(
            str(payload["degraded_reason"])
            if payload.get("degraded_reason") is not None
            else None
        ),
        parent_death_linked=True,
        release_id=str(payload.get("release_id", "")),
    )


def _handle_message(
    raw_line: bytes,
    scopes: dict[str, ProcessScopeSnapshot],
) -> bool:
    """Apply one control message; return True for graceful shutdown."""

    payload = loads_json_object(raw_line.decode("utf-8"))
    if payload is None:
        raise ValueError("watchdog message must be a valid object")
    action = payload.get("action")
    if action == "register":
        scope = _scope_from_wire(payload.get("scope"))
        scopes[scope.release_id] = scope
        return False
    if action == "shutdown":
        return True
    raise ValueError(f"unknown watchdog action: {action!r}")


def watch_parent_scopes(
    *,
    parent: WatchedProcess,
    control_fd: int,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    grace_seconds: float = _DEFAULT_GRACE_SECONDS,
) -> int:
    """Read scope registrations and terminate them if the launcher disappears."""

    scopes: dict[str, ProcessScopeSnapshot] = {}
    buffered = b""
    graceful_shutdown = False
    parent_died = False

    while not graceful_shutdown and not parent_died:
        if not process_is_same_birth(parent):
            parent_died = True
            break
        readable, _, _ = select.select(
            [control_fd],
            [],
            [],
            max(0.05, poll_interval_seconds),
        )
        if not readable:
            continue
        chunk = os.read(control_fd, 64 * 1024)
        if not chunk:
            # The only graceful close is an explicit shutdown message. A bare
            # EOF is a crash, including SIGKILL while the parent is a zombie.
            parent_died = True
            break
        buffered += chunk
        while b"\n" in buffered:
            raw_line, buffered = buffered.split(b"\n", 1)
            if not raw_line:
                continue
            try:
                graceful_shutdown = _handle_message(raw_line, scopes)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                # Fail closed: malformed parent control must not silently drop
                # the launcher's lifetime protection.
                parent_died = True
                break
            if graceful_shutdown:
                break

    if not parent_died:
        return 0

    # Drain registrations already committed to the pipe before EOF so a child
    # started immediately before launcher death is still covered.
    while True:
        chunk = os.read(control_fd, 64 * 1024)
        if not chunk:
            break
        buffered += chunk
    for raw_line in buffered.splitlines():
        if not raw_line:
            continue
        try:
            _handle_message(raw_line, scopes)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            continue

    for scope in reversed(tuple(scopes.values())):
        terminate_scope_sync(
            scope,
            grace_seconds=grace_seconds,
            reason="parent_exit_watchdog",
        )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created-at", type=float, required=True)
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--ready-fd", type=int, default=-1)
    parser.add_argument("--poll-interval", type=float, default=_DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--grace-seconds", type=float, default=_DEFAULT_GRACE_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.ready_fd >= 0:
        try:
            os.write(args.ready_fd, b"1")
        finally:
            os.close(args.ready_fd)
    try:
        return watch_parent_scopes(
            parent=WatchedProcess(
                pid=args.parent_pid,
                created_at_epoch=args.parent_created_at,
            ),
            control_fd=args.control_fd,
            poll_interval_seconds=args.poll_interval,
            grace_seconds=args.grace_seconds,
        )
    finally:
        os.close(args.control_fd)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())


__all__ = [
    "WatchedProcess",
    "main",
    "process_is_same_birth",
    "process_scope_is_alive",
    "watch_parent_scopes",
    "watch_parent_until_exit",
]
