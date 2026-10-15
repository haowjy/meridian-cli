"""Launcher parent-death watchdog for one or more process scopes.

The watchdog is started before a launcher creates children. The launcher then
registers each concrete scope over a pipe. A graceful launcher shutdown sends
an explicit shutdown message; EOF without that message means the launcher died
and every registered scope is terminated.
"""

from __future__ import annotations

import json
import os
import select
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

import psutil

if __package__ or TYPE_CHECKING:
    from meridian.lib.platform.scope_termination import (
        ProcessScope,
        birth_time_unverified,
        terminate_scope_sync,
    )
else:
    from scope_termination import (
        ProcessScope,
        birth_time_unverified,
        terminate_scope_sync,
    )

_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_GRACE_SECONDS = 5.0


class WatchedProcess:
    __slots__ = ("created_at_epoch", "pid")

    def __init__(self, *, pid: int, created_at_epoch: float) -> None:
        self.pid = pid
        self.created_at_epoch = created_at_epoch


class WatchdogScope(ProcessScope):
    """Process-scope fields received over the launcher control pipe."""

    __slots__ = (
        "containment",
        "pgid",
        "release_id",
        "root_created_at_epoch",
        "root_pid",
        "scope_id",
    )

    def __init__(
        self,
        *,
        scope_id: str,
        containment: str,
        root_pid: int,
        root_created_at_epoch: float,
        pgid: int | None,
        release_id: str,
    ) -> None:
        self.scope_id = scope_id
        self.containment = containment
        self.root_pid = root_pid
        self.root_created_at_epoch = root_created_at_epoch
        self.pgid = pgid
        self.release_id = release_id


class WatchdogArguments:
    __slots__ = (
        "control_fd",
        "grace_seconds",
        "parent_created_at",
        "parent_pid",
        "poll_interval",
        "ready_fd",
    )

    def __init__(
        self,
        *,
        parent_pid: int,
        parent_created_at: float,
        control_fd: int,
        ready_fd: int,
        poll_interval: float,
        grace_seconds: float,
    ) -> None:
        self.parent_pid = parent_pid
        self.parent_created_at = parent_created_at
        self.control_fd = control_fd
        self.ready_fd = ready_fd
        self.poll_interval = poll_interval
        self.grace_seconds = grace_seconds


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


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]  # JSON
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _scope_from_wire(payload: object) -> WatchdogScope:
    row = _json_object(payload)
    if row is None:
        raise ValueError("watchdog scope payload must be an object")
    scope_id = str(row["scope_id"])
    root_pid = int(str(row["root_pid"]))
    root_created_at_epoch = float(str(row["root_created_at_epoch"]))
    if birth_time_unverified(root_created_at_epoch):
        raise ValueError("watchdog scope birth time must be known")
    release_id = row["release_id"]
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("watchdog scope release_id must be a non-empty string")
    return WatchdogScope(
        scope_id=scope_id,
        containment=str(row["containment"]),
        root_pid=root_pid,
        root_created_at_epoch=root_created_at_epoch,
        pgid=int(str(row["pgid"])) if row.get("pgid") is not None else None,
        release_id=release_id,
    )


def _handle_message(
    raw_line: bytes,
    scopes: dict[str, WatchdogScope],
) -> bool:
    """Apply one control message; return True for graceful shutdown."""

    decoded: object = json.loads(raw_line.decode("utf-8"))
    payload = _json_object(decoded)
    if payload is None:
        raise ValueError("watchdog message must be a valid object")
    action = payload.get("action")
    if action == "register":
        scope = _scope_from_wire(payload.get("scope"))
        scopes[scope.release_id] = scope
        return False
    if action == "release":
        release_id = payload.get("release_id")
        if not isinstance(release_id, str) or not release_id:
            raise ValueError("watchdog release_id must be a non-empty string")
        scopes.pop(release_id, None)
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

    scopes: dict[str, WatchdogScope] = {}
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
        try:
            terminate_scope_sync(
                scope,
                grace_seconds=grace_seconds,
            )
        except Exception:
            # One broken/stale scope must not prevent remaining registrations
            # from being terminated after their launcher dies.
            continue
    return 0


def _parse_args(argv: Sequence[str] | None) -> WatchdogArguments:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if len(raw_args) % 2:
        raise ValueError("watchdog arguments must be flag/value pairs")
    values = dict(zip(raw_args[::2], raw_args[1::2], strict=True))
    known_flags = {
        "--control-fd",
        "--grace-seconds",
        "--parent-created-at",
        "--parent-pid",
        "--poll-interval",
        "--ready-fd",
    }
    unknown_flags = values.keys() - known_flags
    if unknown_flags:
        raise ValueError(f"unknown watchdog arguments: {sorted(unknown_flags)!r}")
    return WatchdogArguments(
        parent_pid=int(values["--parent-pid"]),
        parent_created_at=float(values["--parent-created-at"]),
        control_fd=int(values["--control-fd"]),
        ready_fd=int(values.get("--ready-fd", "-1")),
        poll_interval=float(values.get("--poll-interval", str(_DEFAULT_POLL_INTERVAL_SECONDS))),
        grace_seconds=float(values.get("--grace-seconds", str(_DEFAULT_GRACE_SECONDS))),
    )


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
    "WatchdogScope",
    "WatchedProcess",
    "main",
    "process_is_same_birth",
    "watch_parent_scopes",
]
