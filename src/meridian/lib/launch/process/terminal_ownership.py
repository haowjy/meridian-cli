"""Exclusive terminal ownership for interactive primary TUIs."""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS


@dataclass(frozen=True)
class TerminalFds:
    """Stable terminal handles retained while the launcher's stdio is captured."""

    stdin: int
    stdout: int
    stderr: int


@dataclass
class TuiTerminalWindow:
    """State exposed to the TUI owner while it exclusively owns the terminal."""

    diagnostics_path: Path
    terminal_fds: TerminalFds | None = None
    captured_path: Path | None = None


def format_launcher_diagnostics_pointer(path: Path) -> str:
    """Return the single post-TUI pointer shown when launcher output was captured."""

    return f"Launcher output during the TUI was written to {path}"


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, ValueError):
            stream.flush()


def _terminal_is_interactive() -> bool:
    try:
        return not IS_WINDOWS and os.isatty(0) and os.isatty(1)
    except OSError:
        return False


@contextmanager
def tui_terminal_window(diagnostics_path: Path) -> Generator[TuiTerminalWindow, None, None]:
    """Give a TUI exclusive terminal ownership and capture every other stdio write.

    The yielded descriptors are duplicates of the real terminal. A relay or native
    TUI must use them instead of process fds 0/1/2, because launcher fds 1 and 2
    point at ``diagnostics_path`` until the context exits.
    """

    window = TuiTerminalWindow(diagnostics_path=diagnostics_path.resolve())
    if not _terminal_is_interactive():
        yield window
        return

    saved_fd_values: list[int] = []
    saved_fds: TerminalFds | None = None
    log_fd = -1
    start_size: int | None = None

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
        start_size = os.fstat(log_fd).st_size

        _flush_standard_streams()
        # stderr is the known logging channel and is redirected first. The TUI is
        # not started or relayed until both replacements are complete.
        os.dup2(log_fd, 2, inheritable=True)
        os.dup2(log_fd, 1, inheritable=True)
        os.close(log_fd)
        log_fd = -1
        window.terminal_fds = saved_fds
        yield window
    finally:
        if saved_fds is not None:
            _flush_standard_streams()
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
        for fd in saved_fd_values:
            with suppress(OSError):
                os.close(fd)
        if log_fd >= 0:
            with suppress(OSError):
                os.close(log_fd)


__all__ = [
    "TerminalFds",
    "TuiTerminalWindow",
    "format_launcher_diagnostics_pointer",
    "tui_terminal_window",
]
