"""Process launcher contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

PRIMARY_STDERR_LOG_PATH_ENV = "_MERIDIAN_PRIMARY_STDERR_LOG_PATH"


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
    "ProcessLauncher",
    "ProcessLauncherSelector",
    "ProcessPlatformContract",
    "ProcessSurfaceMode",
    "RunningProcess",
    "SelectedProcessLauncher",
]
