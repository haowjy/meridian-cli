"""Signal forwarding utilities for run execution.

Also includes process-group helpers for subprocess lifecycle management
(formerly ``exec/process_groups.py``).
"""

import asyncio
import os
import signal
import sys
from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock, RLock
from types import FrameType
from typing import Final, Self, cast

# ---------------------------------------------------------------------------
# Process-group helpers (absorbed from exec/process_groups.py)
# ---------------------------------------------------------------------------


def signal_process_group(
    process: asyncio.subprocess.Process,
    signum: signal.Signals,
) -> None:
    """Send one signal to the subprocess process group.

    On Windows, sends to the process directly because process groups are not
    used for child launch isolation. On POSIX, sends to the process group.

    The child may exit between returncode checks and signal delivery, so
    ProcessLookupError/OSError are treated as expected races.
    """

    if process.returncode is not None:
        return

    pid = process.pid

    try:
        if sys.platform == "win32":
            process.send_signal(signum)
            return
        pgid = os.getpgid(pid)
        os.killpg(pgid, signum)
    except (ProcessLookupError, OSError):
        return


def force_kill_process(process: asyncio.subprocess.Process) -> None:
    """Force-kill a process immediately, handling platform differences.

    On Windows, signal.SIGKILL does not exist, so we use process.kill()
    which calls TerminateProcess(). On POSIX, we send SIGKILL to the
    process group to ensure all child processes are terminated.
    """

    if process.returncode is not None:
        return

    try:
        if sys.platform == "win32":
            process.kill()
            return
        # On POSIX, send SIGKILL to the process group
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return


# ---------------------------------------------------------------------------
# Signal forwarding
# ---------------------------------------------------------------------------

TARGET_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGINT, signal.SIGTERM)


def signal_to_exit_code(received_signal: signal.Signals | None) -> int | None:
    """Map forwarded signal to documented meridian exit code."""

    if received_signal == signal.SIGINT:
        return 130
    if received_signal == signal.SIGTERM:
        return 143
    return None


def map_process_exit_code(
    *,
    raw_return_code: int,
    received_signal: signal.Signals | None,
) -> int:
    """Map raw subprocess return code + forwarded signal to meridian semantics."""

    signaled_exit = signal_to_exit_code(received_signal)
    if signaled_exit is not None:
        return signaled_exit

    if raw_return_code == 0:
        return 0

    if raw_return_code < 0:
        try:
            signum = signal.Signals(-raw_return_code)
        except ValueError:
            return 1
        mapped = signal_to_exit_code(signum)
        if mapped is not None:
            return mapped
    return 1


class SignalForwarder:
    """Scoped SIGINT/SIGTERM forwarding from parent process to child process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._received_signal: signal.Signals | None = None
        self._seen_signal_count = 0

    @property
    def received_signal(self) -> signal.Signals | None:
        return self._received_signal

    def __enter__(self) -> Self:
        signal_coordinator().register_forwarder(self)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        signal_coordinator().unregister_forwarder(self)

    def forward_signal(self, signum: signal.Signals) -> None:
        """Forward one signal to the child and remember it for exit-code mapping."""

        self._received_signal = signum
        self._seen_signal_count += 1

        signal_process_group(self._process, signum)

        if self._seen_signal_count >= 2 and self._process.returncode is None:
            # Second termination signal means "force stop now".
            force_kill_process(self._process)


class SignalCoordinator:
    """Process-global signal demultiplexer for active signal forwarders."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._forwarders: set[SignalForwarder] = set()
        self._previous_handlers: dict[signal.Signals, signal.Handlers] = {}
        self._handlers_installed = False
        self._sigterm_mask_depth = 0

    def register_forwarder(self, forwarder: SignalForwarder) -> None:
        with self._lock:
            self._forwarders.add(forwarder)
            self._ensure_handlers_installed_locked()

    def unregister_forwarder(self, forwarder: SignalForwarder) -> None:
        with self._lock:
            self._forwarders.discard(forwarder)
            self._maybe_uninstall_handlers_locked()

    @contextmanager
    def mask_sigterm(self) -> Generator[None, None, None]:
        """Ignore SIGTERM while executing a critical section."""

        mask_installed = False
        with self._lock:
            if self._ensure_handlers_installed_locked():
                self._sigterm_mask_depth += 1
                mask_installed = True

        try:
            yield
        finally:
            if mask_installed:
                with self._lock:
                    self._sigterm_mask_depth -= 1
                    self._maybe_uninstall_handlers_locked()

    def _ensure_handlers_installed_locked(self) -> bool:
        if self._handlers_installed:
            return True

        previous_handlers: dict[signal.Signals, signal.Handlers] = {}
        try:
            for signum in TARGET_SIGNALS:
                previous_handlers[signum] = cast("signal.Handlers", signal.getsignal(signum))
                signal.signal(signum, self._on_signal)
        except ValueError:
            # Signal handlers can only be changed from the main thread.
            return False

        self._previous_handlers = previous_handlers
        self._handlers_installed = True
        return True

    def _maybe_uninstall_handlers_locked(self) -> None:
        if not self._handlers_installed:
            return
        if self._forwarders or self._sigterm_mask_depth > 0:
            return

        try:
            for signum in TARGET_SIGNALS:
                signal.signal(signum, self._previous_handlers.get(signum, signal.SIG_DFL))
        except ValueError:
            return

        self._handlers_installed = False
        self._previous_handlers.clear()

    def _dispatch_previous_handler(
        self,
        signum: signal.Signals,
        frame: FrameType | None,
        previous_handler: signal.Handlers,
    ) -> None:
        if previous_handler == signal.SIG_IGN:
            return
        if previous_handler == signal.SIG_DFL:
            # Re-emit to preserve default process semantics when no forwarders are active.
            signal.signal(signum, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signum)
            finally:
                with self._lock:
                    if self._handlers_installed:
                        signal.signal(signum, self._on_signal)
            return
        if callable(previous_handler):
            previous_handler(signum.value, frame)

    def _on_signal(self, raw_signum: int, frame: FrameType | None) -> None:
        signum = signal.Signals(raw_signum)
        with self._lock:
            if signum == signal.SIGTERM and self._sigterm_mask_depth > 0:
                return
            forwarders = tuple(self._forwarders)
            previous_handler = self._previous_handlers.get(signum, signal.SIG_DFL)

        if forwarders:
            for forwarder in forwarders:
                forwarder.forward_signal(signum)
            return

        self._dispatch_previous_handler(signum, frame, previous_handler)


_COORDINATOR_LOCK = Lock()
_coordinator: SignalCoordinator | None = None


def signal_coordinator() -> SignalCoordinator:
    """Return the process-global signal coordinator singleton."""

    global _coordinator
    if _coordinator is None:
        with _COORDINATOR_LOCK:
            if _coordinator is None:
                _coordinator = SignalCoordinator()
    return _coordinator
