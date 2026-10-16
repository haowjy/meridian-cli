"""Gated TUI launching on the shell's foreground terminal and process group."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

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
        self.gate.close()
        self.process.poll()

    def cancel_wait(self) -> None:
        # Called inside Python signal handlers; do not acquire Event's lock.
        self.wait_cancelled = True

    def wait(self) -> LaunchedProcess:
        try:
            if self.wait_cancelled:
                self.gate.close()
                return LaunchedProcess(exit_code=self.process.wait(), pid=self.identity.pid)
            self.gate.open()
            while True:
                try:
                    return LaunchedProcess(
                        exit_code=self.process.wait(timeout=0.1), pid=self.identity.pid
                    )
                except subprocess.TimeoutExpired:
                    if self.wait_cancelled:
                        return LaunchedProcess(exit_code=130, pid=self.identity.pid)
        finally:
            self.gate.release_after_reap(self.process.poll())


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
        child_env = dict(env)
        child_env.pop(PRIMARY_STDERR_LOG_PATH_ENV, None)
        gate = LaunchGate(group=False)
        try:
            process = subprocess.Popen(
                **gate.spawn_kwargs(command, child_env, cwd=cwd),
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
