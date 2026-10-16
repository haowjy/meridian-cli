"""Gated TUI launching on the shell's foreground terminal and process group."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.launch.signals import SignalCallbackReceiver, signal_coordinator
from meridian.lib.platform.launch_gate import LaunchGate, ProcessIdentity

from .ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    LaunchedProcess,
    ProcessLauncher,
    TerminalFds,
)


@dataclass
class _RunningNativeProcess:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    gate: LaunchGate
    wait_cancelled: bool = False

    def terminate(self) -> None:
        self.gate.release_after_reap(self.process.poll())

    def cancel_wait(self) -> None:
        # Called inside Python signal handlers; do not acquire Event's lock.
        self.wait_cancelled = True

    def wait(self) -> LaunchedProcess:
        if self.wait_cancelled:
            return LaunchedProcess(exit_code=130, pid=self.identity.pid)
        self.gate.open()
        try:
            while True:
                try:
                    exit_code = self.process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if self.wait_cancelled:
                        return LaunchedProcess(exit_code=130, pid=self.identity.pid)
        except BaseException:
            self.gate.release_after_reap(self.process.poll())
            raise
        self.gate.release_after_reap(exit_code)
        return LaunchedProcess(exit_code=exit_code, pid=self.identity.pid)


class NativeProcessLauncher(ProcessLauncher):
    """Keep the TUI in the shell's job; never target that shared process group."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        terminal_fds: TerminalFds,
    ) -> _RunningNativeProcess:
        if output_log_path is not None:
            raise ValueError("Native terminal launcher does not capture output")
        if PRIMARY_STDERR_LOG_PATH_ENV in env:
            raise ValueError("Native terminal launcher does not capture stderr")
        gate = LaunchGate(group=False)
        try:
            process = subprocess.Popen(
                **gate.spawn_kwargs(command, env, cwd=cwd),
                cwd=cwd,
                stdin=terminal_fds.stdin,
                stdout=terminal_fds.stdout,
                stderr=terminal_fds.stderr,
            )
            identity = gate.adopt(process.pid)
        except BaseException:
            gate.close()
            raise
        return _RunningNativeProcess(process=process, identity=identity, gate=gate)


@contextmanager
def native_sigint_shield(launcher: ProcessLauncher) -> Generator[None]:
    """Enter on the main thread while a native TUI owns the foreground job.

    A Python receiver, unlike SIG_IGN, leaves SIGINT at its default on exec.
    """
    if not isinstance(launcher, NativeProcessLauncher):
        yield
        return
    receiver = SignalCallbackReceiver(
        target_signals=(signal.SIGINT,),
        callback=lambda _signum: None,
    )
    coordinator = signal_coordinator()
    coordinator.register_receiver(receiver)
    try:
        yield
    finally:
        coordinator.unregister_receiver(receiver)
