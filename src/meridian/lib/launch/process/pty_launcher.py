"""PTY-backed process launching."""

from __future__ import annotations

import os
import re
import signal
import struct
import sys
import threading
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from meridian.lib.platform import IS_WINDOWS, fcntl, pty, select, termios, tty

from .ports import LaunchedProcess, ProcessLauncher
from .terminal_ownership import TerminalFds

_MOUSE_MODE_RESET_SEQUENCE = (
    b"\x1b[?1000l"
    b"\x1b[?1002l"
    b"\x1b[?1003l"
    b"\x1b[?1006l"
)
_ALTERNATE_SCREEN_EXIT_SEQUENCE = b"\x1b[?1049l"
_CURSOR_SHOW_SEQUENCE = b"\x1b[?25h"
_ALTERNATE_SCREEN_TRANSITION = re.compile(rb"\x1b\[\?(?:47|1047|1049)([hl])")
_ALTERNATE_SCREEN_TAIL_LENGTH = len(_ALTERNATE_SCREEN_EXIT_SEQUENCE) - 1


@dataclass
class _AlternateScreenState:
    """Track the child's alternate-screen state across PTY read boundaries."""

    enabled: bool = False
    _tail: bytes = b""

    def observe(self, chunk: bytes) -> None:
        data = self._tail + chunk
        for match in _ALTERNATE_SCREEN_TRANSITION.finditer(data):
            self.enabled = match.group(1) == b"h"
        self._tail = data[-_ALTERNATE_SCREEN_TAIL_LENGTH:]


def can_use_pty() -> bool:
    return not IS_WINDOWS and sys.stdin.isatty() and sys.stdout.isatty()


def _read_winsize(fd: int) -> bytes | None:
    """Return packed winsize bytes for one terminal fd, or None if unavailable."""

    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def _sync_pty_winsize(*, source_fd: int, target_fd: int) -> None:
    """Copy the current terminal winsize onto the PTY master."""

    winsize = _read_winsize(source_fd)
    if winsize is None:
        return
    try:
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        return


def _copy_primary_pty_output(
    *,
    child_pid: int,
    master_fd: int,
    output_log_path: Path | None,
    terminal_fds: TerminalFds | None = None,
    wait_cancelled: threading.Event | None = None,
) -> int:
    if terminal_fds is None:
        terminal_fds = TerminalFds(
            stdin=sys.stdin.fileno(),
            stdout=sys.stdout.fileno(),
            stderr=sys.stderr.fileno(),
        )
    stdin_fd = terminal_fds.stdin
    stdout_fd = terminal_fds.stdout
    stdin_open = True
    saved_tty_attrs: Any = None
    last_forwarded_winsize: bytes | None = None
    alternate_screen = _AlternateScreenState()

    try:
        if os.isatty(stdin_fd):
            saved_tty_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        with ExitStack() as stack:
            output_handle: BinaryIO | None = None
            if output_log_path is not None:
                output_log_path.parent.mkdir(parents=True, exist_ok=True)
                output_handle = stack.enter_context(output_log_path.open("wb"))

            while True:
                if wait_cancelled is not None and wait_cancelled.is_set():
                    return 130
                winsize = _read_winsize(stdout_fd)
                if winsize is not None and winsize != last_forwarded_winsize:
                    try:
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                    except OSError:
                        pass
                    else:
                        last_forwarded_winsize = winsize
                fds = [master_fd]
                if stdin_open:
                    fds.append(stdin_fd)
                ready, _, _ = select.select(fds, [], [], 0.1)

                if master_fd in ready:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    if output_handle is not None:
                        output_handle.write(chunk)
                        output_handle.flush()
                    alternate_screen.observe(chunk)
                    try:
                        os.write(stdout_fd, chunk)
                    except OSError:
                        break

                if stdin_open and stdin_fd in ready:
                    try:
                        data = os.read(stdin_fd, 1024)
                    except OSError:
                        stdin_open = False
                        continue
                    if not data:
                        stdin_open = False
                    else:
                        try:
                            os.write(master_fd, data)
                        except OSError:
                            stdin_open = False
                            continue
    finally:
        if saved_tty_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_tty_attrs)
        if os.isatty(stdout_fd):
            with suppress(OSError):
                os.write(
                    stdout_fd,
                    _MOUSE_MODE_RESET_SEQUENCE
                    + (
                        _ALTERNATE_SCREEN_EXIT_SEQUENCE
                        if alternate_screen.enabled
                        else b""
                    )
                    + _CURSOR_SHOW_SEQUENCE,
                )

    if wait_cancelled is not None and wait_cancelled.is_set():
        return 130
    _, status = os.waitpid(child_pid, 0)
    return os.waitstatus_to_exitcode(status)


@dataclass
class _RunningPtyProcess:
    pid: int
    master_fd: int
    output_log_path: Path | None
    terminal_fds: TerminalFds | None
    wait_cancelled: threading.Event = field(default_factory=threading.Event)

    def wait(self) -> LaunchedProcess:
        try:
            exit_code = _copy_primary_pty_output(
                child_pid=self.pid,
                master_fd=self.master_fd,
                output_log_path=self.output_log_path,
                terminal_fds=self.terminal_fds,
                wait_cancelled=self.wait_cancelled,
            )
            return LaunchedProcess(
                exit_code=exit_code,
                pid=self.pid,
            )
        finally:
            with suppress(OSError):
                os.close(self.master_fd)

    def terminate(self) -> None:
        with suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)

    def cancel_wait(self) -> None:
        self.wait_cancelled.set()


class PtyProcessLauncher(ProcessLauncher):
    """Unix PTY launcher with optional output capture."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        terminal_fds: TerminalFds | None = None,
    ) -> _RunningPtyProcess:
        if IS_WINDOWS:
            raise RuntimeError("PTY launcher is not available on Windows")

        master_fd, slave_fd = pty.openpty()
        terminal_stdout_fd = (
            terminal_fds.stdout if terminal_fds is not None else sys.stdout.fileno()
        )
        _sync_pty_winsize(source_fd=terminal_stdout_fd, target_fd=master_fd)

        child_pid = os.fork()
        if child_pid == 0:
            # The parent may have installed temporary teardown barriers. Do not
            # carry them across login_tty, where an inherited handler traceback
            # would otherwise be able to write onto the TUI terminal.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            try:
                os.close(master_fd)
                os.login_tty(slave_fd)
                os.chdir(cwd)
                os.execvpe(command[0], command, env)
            except FileNotFoundError:
                os._exit(127)
            except Exception:
                os._exit(1)

        os.close(slave_fd)
        return _RunningPtyProcess(
            pid=child_pid,
            master_fd=master_fd,
            output_log_path=output_log_path,
            terminal_fds=terminal_fds,
        )


__all__ = [
    "PtyProcessLauncher",
    "_sync_pty_winsize",
    "can_use_pty",
]
