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

import os
import shutil
import signal
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import TypedDict

import psutil

from meridian.lib.platform import IS_WINDOWS

GATE_STUB = r"""
fd=$_MERIDIAN_GATE_FD; [ "$fd" = 9 ] || exec 9</dev/fd/"$fd"
scope=$_MERIDIAN_GATE_SCOPE; grace=$_MERIDIAN_GATE_GRACE; h=$$
unset _MERIDIAN_GATE_FD _MERIDIAN_GATE_SCOPE _MERIDIAN_GATE_GRACE
IFS= read -r g <&9 || exit 125; [ "$g" = go ] || exit 125
( ( exec </dev/null >/dev/null 2>&1; trap '' HUP; IFS= read -r m <&9; [ "$m" = stop ] && exit 0
    if [ "$scope" = group ]; then
      PATH=/usr/bin:/bin; export PATH; trap '' TERM; kill -TERM 0; sleep "$grace"; kill -KILL 0
    else
      kill -TERM "$h"
    fi ) & )
exec 9<&-; exec "$@"
"""


def child_birth_epoch(pid: int) -> float:
    """Read a blocked, unreaped child's birth; unknown is a launch failure."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        raise RuntimeError(f"Cannot read launch child {pid} birth time") from exc


@dataclass(frozen=True)
class ProcessIdentity:
    """Identity captured while the child is blocked, before harness exec."""

    pid: int
    birth_epoch: float
    pgid: int | None


class SpawnKwargs(TypedDict):
    args: tuple[str, ...]
    env: dict[str, str]
    pass_fds: tuple[int, ...]
    start_new_session: bool


class LaunchGate:
    """Own the runner side of the protocol; a no-op on legacy Windows."""

    def __init__(self, *, group: bool = True, grace_seconds: float = 2.0) -> None:
        self._read_fd, self._write_fd = (-1, -1) if IS_WINDOWS else os.pipe()
        self._lock = Lock()
        self.group = group
        self.grace_seconds = grace_seconds

    @property
    def read_fd(self) -> int:
        return self._read_fd

    def spawn_kwargs(
        self, command: Sequence[str], env: Mapping[str, str], *, cwd: str | os.PathLike[str] = "."
    ) -> SpawnKwargs:
        # Resolve against the child's PATH and cwd, not the runner's environment.
        # Otherwise /bin/sh masks a missing harness as an ordinary exit 127.
        search_path = os.pathsep.join(
            os.path.abspath(os.path.join(cwd, entry)) for entry in os.get_exec_path(dict(env))
        )
        binary = command[0]
        target = os.path.join(cwd, binary) if os.path.dirname(binary) else binary
        resolved = shutil.which(target, path=search_path)
        if resolved is None:
            raise FileNotFoundError(2, os.strerror(2), binary)
        resolved_command = (os.path.abspath(resolved), *command[1:])
        return SpawnKwargs(
            args=self.command(resolved_command),
            env=self.environment(env),
            pass_fds=() if IS_WINDOWS else (self.read_fd,),
            start_new_session=not IS_WINDOWS and self.group,
        )

    def adopt(self, pid: int) -> ProcessIdentity:
        """Capture a blocked child's identity, or kill and reap on failure."""
        self.close_read_end()
        try:
            return ProcessIdentity(
                pid, child_birth_epoch(pid), pid if self.group and not IS_WINDOWS else None
            )
        except BaseException:
            self.close()
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(pid, 0)
            raise

    def command(self, command: Sequence[str]) -> tuple[str, ...]:
        if IS_WINDOWS:
            return tuple(command)
        return ("/bin/sh", "-c", GATE_STUB, "meridian-launch-gate", *command)

    def environment(self, env: Mapping[str, str]) -> dict[str, str]:
        if IS_WINDOWS:
            return dict(env)
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
        if self._read_fd >= 0:
            os.close(self._read_fd)
            self._read_fd = -1

    def open(self) -> None:
        """Release the harness after its scope is durable."""
        # Primary wait and cancellation run in different threads. Never write
        # a descriptor after close (another launch may already have reused it).
        with self._lock:
            if self._write_fd >= 0:
                with suppress(BrokenPipeError):
                    os.write(self._write_fd, b"go\n")

    def release_after_reap(self, returncode: int | None) -> None:
        """Disarm only for an observed child exit; otherwise fail closed."""
        if returncode is not None:
            self.stop()
        else:
            self.close()

    def stop(self) -> None:
        """Disarm the guardian after harness exit, then close the pipe."""
        with self._lock:
            if self._write_fd >= 0:
                with suppress(BrokenPipeError):
                    os.write(self._write_fd, b"stop\n")
            self._close()

    def close(self) -> None:
        """Runner death = kill: EOF before go exits 125, after go kills the scope.

        Do not also TERM the root: root-only mode must send exactly one TERM.
        """
        with self._lock:
            self._close()

    def _close(self) -> None:
        self._close_read_end()
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1
