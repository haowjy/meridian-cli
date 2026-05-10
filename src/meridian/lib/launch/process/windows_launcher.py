"""Windows-specific process launching fallbacks."""

from __future__ import annotations

import logging
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS

from .ports import ChildStartedHook, LaunchedProcess, ProcessLauncher
from .subprocess_launcher import SubprocessProcessLauncher

logger = logging.getLogger(__name__)


def can_use_windows_console_launcher() -> bool:
    """Return whether Windows console-inheritance fallback should be used."""

    return IS_WINDOWS


class WindowsConsoleLauncher(ProcessLauncher):
    """Windows fallback launcher that preserves interactive console semantics."""

    def launch(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: ChildStartedHook | None = None,
    ) -> LaunchedProcess:
        logger.debug("Windows console-inheritance mode (no PTY capture available)")
        return SubprocessProcessLauncher().launch(
            command=command,
            cwd=cwd,
            env=env,
            output_log_path=output_log_path,
            on_child_started=on_child_started,
        )


__all__ = ["WindowsConsoleLauncher", "can_use_windows_console_launcher"]
