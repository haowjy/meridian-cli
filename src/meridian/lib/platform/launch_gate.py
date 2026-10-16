"""POSIX start gate and EOF-driven, double-forked process-group guardian.

The runner alone owns the close-on-exec pipe writer. Call ``open`` only after
persisting the blocked child's scope, and ``stop`` only after reaping it.
Closing without stop is runner death to the guardian.

Dash cannot close arbitrary multi-digit descriptors. Reopening the inherited
read end onto fd 9 leaves the original read end in the harness. This cannot
prevent EOF (only writers do); only the guardian reads after the go handshake.
The guardian drops all harness stdio before waiting, allowing PTY/pipe EOF
before the runner reaps the harness and sends stop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from contextlib import suppress
from threading import Lock

import psutil

GATE_STUB = r'''
fd=$_MERIDIAN_GATE_FD; [ "$fd" = 9 ] || exec 9</dev/fd/"$fd"
scope=$_MERIDIAN_GATE_SCOPE; h=$$
unset _MERIDIAN_GATE_FD _MERIDIAN_GATE_SCOPE
IFS= read -r g <&9 || exit 125; [ "$g" = go ] || exit 125
( ( exec </dev/null >/dev/null 2>&1; trap '' HUP; IFS= read -r m <&9; [ "$m" = stop ] && exit 0
    if [ "$scope" = group ]; then
      trap '' TERM; kill -TERM 0; sleep "$_MERIDIAN_GATE_GRACE"; kill -KILL 0
    else
      kill -TERM "$h"
    fi ) & )
exec 9<&-; exec "$@"
'''


def child_birth_epoch(pid: int) -> float:
    """Read a blocked, unreaped child's birth; unknown is a launch failure."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        raise RuntimeError(f"Cannot read launch child {pid} birth time") from exc


class LaunchGate:
    """Own the runner side of the start/stop protocol, never a child writer."""

    def __init__(self, *, group: bool = True, grace_seconds: float = 2.0) -> None:
        self.read_fd, self._write_fd = os.pipe()
        self._lock = Lock()
        self.group = group
        self.grace_seconds = grace_seconds
        self._exit_task: asyncio.Task[None] | None = None

    def command(self, command: Sequence[str]) -> tuple[str, ...]:
        return ("/bin/sh", "-c", GATE_STUB, "meridian-launch-gate", *command)

    def environment(self, env: Mapping[str, str]) -> dict[str, str]:
        return {
            **env,
            "_MERIDIAN_GATE_FD": str(self.read_fd),
            "_MERIDIAN_GATE_SCOPE": "group" if self.group else "root",
            "_MERIDIAN_GATE_GRACE": str(self.grace_seconds),
        }

    def prepare_pty_exec(self) -> None:
        """In the fork child, match Popen's close_fds/pass_fds isolation."""
        os.closerange(3, self.read_fd)
        os.closerange(self.read_fd + 1, os.sysconf("SC_OPEN_MAX"))
        os.set_inheritable(self.read_fd, True)

    def close_read_end(self) -> None:
        with self._lock:
            self._close_read_end()

    def _close_read_end(self) -> None:
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1

    def open(self) -> None:
        """Release the harness after its scope is durable."""
        # Primary wait and cancellation run in different threads. Never write
        # a descriptor after close (another launch may already have reused it).
        with self._lock:
            self._close_read_end()
            if self._write_fd >= 0:
                os.write(self._write_fd, b"go\n")

    def watch_exit(self, process: asyncio.subprocess.Process) -> None:
        """Disarm after asyncio observes both child exit and output EOF."""
        async def wait() -> None:
            try:
                await process.wait()
                self.stop()
            finally:
                self.close()

        self._exit_task = asyncio.create_task(wait(), name=f"launch-gate-{process.pid}")

    def stop(self) -> None:
        """Disarm the guardian after harness exit, then close the pipe."""
        with self._lock:
            if self._write_fd >= 0:
                with suppress(BrokenPipeError):
                    os.write(self._write_fd, b"stop\n")
            self._close()

    def close(self) -> None:
        """Fail closed: no go means no harness; EOF after go triggers cleanup."""
        with self._lock:
            self._close()

    def _close(self) -> None:
        self._close_read_end()
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1
