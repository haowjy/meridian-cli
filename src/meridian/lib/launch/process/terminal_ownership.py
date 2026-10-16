"""Exclusive terminal ownership for interactive primary TUIs."""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .ports import TerminalFds
from .pty_launcher import can_use_pty


@dataclass
class TuiTerminalWindow:
    """State exposed to the TUI owner while it exclusively owns the terminal."""

    diagnostics_path: Path
    terminal_fds: TerminalFds
    captured_path: Path | None = None


def format_launcher_diagnostics_pointer(path: Path) -> str:
    """Return the single post-TUI pointer shown when launcher output was captured."""

    return f"Launcher output during the TUI was written to {path}"


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, ValueError):
            stream.flush()


@contextmanager
def tui_terminal_window(diagnostics_path: Path) -> Generator[TuiTerminalWindow, None, None]:
    """Give a TUI exclusive terminal ownership and capture every other stdio write.

    The yielded descriptors are duplicates of the real terminal. A relay or native
    TUI must use them instead of process fds 0/1/2, because launcher fd 0 points
    at ``/dev/null`` and fds 1 and 2 point at ``diagnostics_path`` until the
    context exits. A write racing restoration may split across the descriptor
    swap; that is accepted because the TUI has already exited by then.
    """

    window = TuiTerminalWindow(
        diagnostics_path=diagnostics_path.resolve(),
        terminal_fds=TerminalFds(stdin=0, stdout=1, stderr=2),
    )
    if not can_use_pty():
        yield window
        return

    saved_fd_values: list[int] = []
    saved_fds: TerminalFds | None = None
    log_fd = -1
    null_fd = -1
    start_size: int | None = None
    exception_escaping = False

    try:
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        for fd in (0, 1, 2):
            saved_fd_values.append(os.dup(fd))
        saved_fds = TerminalFds(
            stdin=saved_fd_values[0],
            stdout=saved_fd_values[1],
            stderr=saved_fd_values[2],
        )
        log_fd = os.open(
            diagnostics_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        null_fd = os.open(os.devnull, os.O_RDONLY)
        start_size = os.fstat(log_fd).st_size

        _flush_standard_streams()
        # The TUI is not started or relayed until all replacements are complete.
        os.dup2(null_fd, 0, inheritable=True)
        os.dup2(log_fd, 2, inheritable=True)
        os.dup2(log_fd, 1, inheritable=True)
        os.close(null_fd)
        null_fd = -1
        os.close(log_fd)
        log_fd = -1
        window.terminal_fds = saved_fds
        yield window
    except BaseException:
        exception_escaping = True
        raise
    finally:
        if saved_fds is not None:
            _flush_standard_streams()
            with suppress(OSError):
                os.dup2(saved_fds.stdin, 0, inheritable=True)
            with suppress(OSError):
                os.dup2(saved_fds.stdout, 1, inheritable=True)
            with suppress(OSError):
                os.dup2(saved_fds.stderr, 2, inheritable=True)
        if start_size is not None:
            try:
                if window.diagnostics_path.stat().st_size > start_size:
                    window.captured_path = window.diagnostics_path
            except OSError:
                pass
        if exception_escaping and window.captured_path is not None:
            with suppress(BrokenPipeError, OSError, ValueError):
                print(
                    format_launcher_diagnostics_pointer(window.captured_path),
                    file=sys.stderr,
                    flush=True,
                )
        for fd in saved_fd_values:
            with suppress(OSError):
                os.close(fd)
        if log_fd >= 0:
            with suppress(OSError):
                os.close(log_fd)
        if null_fd >= 0:
            with suppress(OSError):
                os.close(null_fd)


__all__ = [
    "TuiTerminalWindow",
    "format_launcher_diagnostics_pointer",
    "tui_terminal_window",
]
