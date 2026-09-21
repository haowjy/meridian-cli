"""Signal forwarding utilities for run execution.

`SignalCoordinator` is the process-global seam that installs handlers for the
union of its registered receivers' target signals and dispatches each delivered
signal to the receivers that target it. `SignalForwarder` forwards
SIGINT/SIGTERM to a subprocess (streaming path); `SignalCallbackReceiver` invokes
a callback (e.g. the managed-primary launcher cancelling its TUI relay).

Also includes process-group helpers for subprocess lifecycle management
(formerly ``exec/process_groups.py``).
"""

import asyncio
import os
import signal
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import Lock, RLock, current_thread, main_thread
from types import FrameType
from typing import Final, Protocol, Self, cast

from meridian.lib.platform import IS_WINDOWS

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
        if IS_WINDOWS:
            process.send_signal(signum)
            return
        pgid = os.getpgid(pid)
        if pgid == pid:
            os.killpg(pgid, signum)
        else:
            process.send_signal(signum)
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
        if IS_WINDOWS:
            process.kill()
            return
        # On POSIX, send SIGKILL to the process group
        pgid = os.getpgid(process.pid)
        if pgid == process.pid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            process.kill()
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


class SignalReceiver(Protocol):
    """A sink registered with :class:`SignalCoordinator`.

    Receivers declare the signals they care about; the coordinator installs
    handlers for the union of all active receivers' targets and dispatches each
    delivered signal to the receivers that target it.
    """

    @property
    def target_signals(self) -> tuple[signal.Signals, ...]: ...

    def forward_signal(self, signum: signal.Signals) -> None: ...


class SignalForwarder:
    """Scoped SIGINT/SIGTERM forwarding from parent process to child process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._received_signal: signal.Signals | None = None
        self._seen_signal_count = 0

    @property
    def received_signal(self) -> signal.Signals | None:
        return self._received_signal

    @property
    def target_signals(self) -> tuple[signal.Signals, ...]:
        return TARGET_SIGNALS

    def __enter__(self) -> Self:
        signal_coordinator().register_receiver(self)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        signal_coordinator().unregister_receiver(self)

    def forward_signal(self, signum: signal.Signals) -> None:
        """Forward one signal to the child and remember it for exit-code mapping."""

        self._received_signal = signum
        self._seen_signal_count += 1

        signal_process_group(self._process, signum)

        if self._seen_signal_count >= 2 and self._process.returncode is None:
            # Second termination signal means "force stop now".
            force_kill_process(self._process)


class SignalCallbackReceiver:
    """Receiver that invokes a callback for a set of signals.

    With ``escalate_on_repeat`` the first signal invokes the callback and a
    later repeat of the same number restores the default disposition and
    re-raises, so a process stuck after the callback returns is force-quit
    instead of remaining trapped. A repeat only escalates when it arrives at
    least ``escalate_repeat_grace_secs`` after the first signal of the same
    number; a rapid burst (e.g. terminal close) is routed to the callback
    instead.
    """

    def __init__(
        self,
        *,
        target_signals: tuple[signal.Signals, ...],
        callback: Callable[[signal.Signals], None],
        escalate_on_repeat: bool = False,
        escalate_repeat_grace_secs: float = 1.0,
    ) -> None:
        self._target_signals = tuple(target_signals)
        self._callback = callback
        self._escalate_on_repeat = escalate_on_repeat
        self._escalate_repeat_grace_secs = escalate_repeat_grace_secs
        self._first_seen_seconds: dict[signal.Signals, float] = {}

    @property
    def target_signals(self) -> tuple[signal.Signals, ...]:
        return self._target_signals

    def forward_signal(self, signum: signal.Signals) -> None:
        if self._escalate_on_repeat:
            now = time.monotonic()
            first_seen = self._first_seen_seconds.get(signum)
            if first_seen is None:
                self._first_seen_seconds[signum] = now
            elif now - first_seen >= self._escalate_repeat_grace_secs:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
                return
        self._callback(signum)


class SignalCoordinator:
    """Process-global signal demultiplexer for active receivers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._receivers: set[SignalReceiver] = set()
        self._previous_handlers: dict[signal.Signals, signal.Handlers] = {}
        self._installed_signals: set[signal.Signals] = set()
        self._sigterm_mask_depth = 0

    def register_receiver(self, receiver: SignalReceiver) -> None:
        with self._lock:
            self._receivers.add(receiver)
            self._reconcile_handlers_locked()

    def unregister_receiver(self, receiver: SignalReceiver) -> None:
        with self._lock:
            self._receivers.discard(receiver)
            self._reconcile_handlers_locked()

    @contextmanager
    def mask_sigterm(self) -> Generator[None, None, None]:
        """Ignore SIGTERM while executing a critical section."""

        with self._lock:
            self._sigterm_mask_depth += 1
            self._reconcile_handlers_locked()
        try:
            yield
        finally:
            with self._lock:
                self._sigterm_mask_depth -= 1
                self._reconcile_handlers_locked()

    def _desired_signals_locked(self) -> set[signal.Signals]:
        desired: set[signal.Signals] = set()
        for receiver in self._receivers:
            desired.update(receiver.target_signals)
        if self._sigterm_mask_depth > 0:
            desired.update(TARGET_SIGNALS)
        return desired

    def _reconcile_handlers_locked(self) -> None:
        """Install handlers for active receivers; restore the rest."""

        desired = self._desired_signals_locked()
        if desired == self._installed_signals:
            return

        if current_thread() is not main_thread():
            # Signal handlers can only be changed from the main thread.
            return

        for signum in desired - self._installed_signals:
            self._previous_handlers[signum] = cast("signal.Handlers", signal.getsignal(signum))
            signal.signal(signum, self._on_signal)
            self._installed_signals.add(signum)
        for signum in self._installed_signals - desired:
            signal.signal(signum, self._previous_handlers.pop(signum, signal.SIG_DFL))
            self._installed_signals.discard(signum)

    def _dispatch_previous_handler(
        self,
        signum: signal.Signals,
        frame: FrameType | None,
        previous_handler: signal.Handlers,
    ) -> None:
        if previous_handler == signal.SIG_IGN:
            return
        if previous_handler == signal.SIG_DFL:
            # Re-emit to preserve default process semantics when no receivers are active.
            signal.signal(signum, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signum)
            finally:
                with self._lock:
                    if signum in self._installed_signals:
                        signal.signal(signum, self._on_signal)
            return
        if callable(previous_handler):
            previous_handler(signum.value, frame)

    def _on_signal(self, raw_signum: int, frame: FrameType | None) -> None:
        signum = signal.Signals(raw_signum)
        with self._lock:
            if signum == signal.SIGTERM and self._sigterm_mask_depth > 0:
                return
            receivers = tuple(
                receiver for receiver in self._receivers if signum in receiver.target_signals
            )
            previous_handler = self._previous_handlers.get(signum, signal.SIG_DFL)

        if receivers:
            for receiver in receivers:
                receiver.forward_signal(signum)
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
