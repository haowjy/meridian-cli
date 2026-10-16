"""Process launcher contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from meridian.lib.platform.process_scope import ProcessScopeSnapshot

PRIMARY_STDERR_LOG_PATH_ENV = "_MERIDIAN_PRIMARY_STDERR_LOG_PATH"


@dataclass(frozen=True)
class ProcessIdentity:
    """Identity captured while the child is blocked, before harness exec."""

    pid: int
    birth_epoch: float
    pgid: int | None

    def scope(self, *, owner_id: str, owner_policy: str = "spawn_owned") -> ProcessScopeSnapshot:
        return ProcessScopeSnapshot(
            scope_id="tui",
            owner_policy=owner_policy,
            owner_id=owner_id,
            role="harness_tui",
            containment="posix_pgid" if self.pgid is not None else "pid_tree_fallback",
            root_pid=self.pid,
            root_created_at_epoch=self.birth_epoch,
            pgid=self.pgid,
            job_name=None,
            degraded_reason=None,
        )


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


class ProcessBackendId(StrEnum):
    """Named launch backends used for local harness process execution."""

    PTY = "pty"
    SUBPROCESS = "subprocess"
    WINDOWS_CONSOLE = "windows_console"


class ProcessSurfaceMode(StrEnum):
    """How the local launcher surfaces child terminal IO."""

    PTY_MEDIATED = "pty_mediated"
    PIPE_CAPTURE = "pipe_capture"
    NATIVE_INHERIT = "native_inherit"


@dataclass(frozen=True)
class ProcessPlatformContract:
    """Explicit process/platform contract for one launcher backend."""

    backend_id: ProcessBackendId
    surface_mode: ProcessSurfaceMode
    captures_output_to_artifact: bool
    platform_family: str


class RunningProcess(Protocol):
    """A started primary process whose blocking exit wait is a separate phase."""

    @property
    def pid(self) -> int: ...

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


@dataclass(frozen=True)
class SelectedProcessLauncher:
    """Launcher instance paired with its explicit process/platform contract."""

    launcher: ProcessLauncher
    contract: ProcessPlatformContract


ProcessLauncherSelector = Callable[[Path | None], ProcessLauncher]


__all__ = [
    "PRIMARY_STDERR_LOG_PATH_ENV",
    "LaunchedProcess",
    "ProcessBackendId",
    "ProcessIdentity",
    "ProcessLauncher",
    "ProcessLauncherSelector",
    "ProcessPlatformContract",
    "ProcessSurfaceMode",
    "RunningProcess",
    "SelectedProcessLauncher",
    "TerminalFds",
]
