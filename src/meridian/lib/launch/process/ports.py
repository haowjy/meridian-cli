"""Process launcher contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meridian.lib.platform.launch_gate import ProcessIdentity

TUI_TERMINATE_GRACE_SECONDS = 5.0

PRIMARY_STDERR_LOG_PATH_ENV = "_MERIDIAN_PRIMARY_STDERR_LOG_PATH"


@dataclass(frozen=True)
class TerminalFds:
    """Terminal descriptors a launched TUI must use for its stdio."""

    stdin: int
    stdout: int
    stderr: int


@dataclass(frozen=True)
class LaunchedProcess:
    """Completed process launch result."""

    exit_code: int
    pid: int | None


class RunningProcess(Protocol):
    """A started primary process whose blocking exit wait is a separate phase."""

    @property
    def identity(self) -> ProcessIdentity: ...

    def wait(self) -> LaunchedProcess: ...

    def terminate(self) -> None: ...

    def cancel_wait(self) -> None:
        """Unblock a concurrent wait without assuming the child exited."""
        ...


class ProcessLauncher(Protocol):
    """Start a primary process and return as soon as its PID is available."""

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        terminal_fds: TerminalFds,
    ) -> RunningProcess: ...
