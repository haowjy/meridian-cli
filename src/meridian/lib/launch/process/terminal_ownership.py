"""Exclusive terminal ownership for interactive primary TUIs."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import cast

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


class _TerminalSignal(BaseException):
    """Defer a default fatal signal until the terminal fds are restored."""

    def __init__(self, signum: signal.Signals) -> None:
        self.signum = signum


def format_launcher_diagnostics_pointer(path: Path) -> str:
    """Return the single post-TUI pointer shown when launcher output was captured."""

    return f"Launcher output during the TUI was written to {path}"


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, ValueError):
            stream.flush()
    # Extensions that use C stdio retain their own buffers independently of
    # Python's TextIOWrapper. Empty all of them while their bytes still target
    # the intended side of the fd swap.
    with suppress(AttributeError, OSError):
        fflush = ctypes.CDLL(None).fflush
        fflush.argtypes = [ctypes.c_void_p]
        fflush.restype = ctypes.c_int
        fflush(None)


def _terminal_is_interactive() -> bool:
    try:
        return not IS_WINDOWS and os.isatty(0) and os.isatty(1)
    except OSError:
        return False


def _install_fatal_signal_barrier() -> dict[signal.Signals, signal.Handlers]:
    """Delay default fatal signals so context teardown can restore process stdio."""

    if threading.current_thread() is not threading.main_thread():
        return {}

    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def _defer(signum: int, _frame: FrameType | None) -> None:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP})
        raise _TerminalSignal(signal.Signals(signum))

    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous = cast("signal.Handlers", signal.getsignal(signum))
        if previous != signal.SIG_DFL:
            continue
        signal.signal(signum, _defer)
        previous_handlers[signum] = previous
    return previous_handlers


def _restore_signal_handlers(
    previous_handlers: dict[signal.Signals, signal.Handlers],
) -> None:
    for signum, previous in previous_handlers.items():
        signal.signal(signum, previous)


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

    fatal_signals = {signal.SIGTERM, signal.SIGHUP}
    signals_blocked = False
    previous_mask: set[signal.Signals] | None = None
    saved_fd_values: list[int] = []
    saved_fds: TerminalFds | None = None
    log_fd = -1
    start_size: int | None = None
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}
    stdout_redirected = False
    stderr_redirected = False
    deferred_signal: signal.Signals | None = None

    try:
        # Keep default-fatal signals pending while descriptors and handlers are in
        # a partially initialized state. They are unblocked only after the cleanup
        # region is fully established.
        if threading.current_thread() is threading.main_thread():
            previous_mask = cast(
                "set[signal.Signals]",
                signal.pthread_sigmask(signal.SIG_BLOCK, fatal_signals),
            )
            signals_blocked = True

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
        previous_handlers = _install_fatal_signal_barrier()

        _flush_standard_streams()
        # stderr is the known logging channel and is redirected first. The TUI is
        # not started or relayed until both replacements are complete.
        os.dup2(log_fd, 2, inheritable=True)
        stderr_redirected = True
        os.dup2(log_fd, 1, inheritable=True)
        stdout_redirected = True
        os.close(log_fd)
        log_fd = -1
        window.terminal_fds = saved_fds

        if previous_mask is not None:
            signals_blocked = False
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield window
    except _TerminalSignal as exc:
        deferred_signal = exc.signum
    finally:
        if previous_mask is not None and not signals_blocked:
            signal.pthread_sigmask(signal.SIG_BLOCK, fatal_signals)
            signals_blocked = True
        if saved_fds is not None:
            _flush_standard_streams()
            if stdout_redirected:
                with suppress(OSError):
                    os.dup2(saved_fds.stdout, 1, inheritable=True)
            if stderr_redirected:
                with suppress(OSError):
                    os.dup2(saved_fds.stderr, 2, inheritable=True)
        if start_size is not None:
            try:
                if window.diagnostics_path.stat().st_size > start_size:
                    window.captured_path = window.diagnostics_path
            except OSError:
                pass
        _restore_signal_handlers(previous_handlers)
        for fd in saved_fd_values:
            with suppress(OSError):
                os.close(fd)
        if log_fd >= 0:
            with suppress(OSError):
                os.close(log_fd)
        if previous_mask is not None and signals_blocked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    if deferred_signal is not None:
        signal.signal(deferred_signal, signal.SIG_DFL)
        os.kill(os.getpid(), deferred_signal)


__all__ = [
    "TerminalFds",
    "TuiTerminalWindow",
    "format_launcher_diagnostics_pointer",
    "tui_terminal_window",
]
