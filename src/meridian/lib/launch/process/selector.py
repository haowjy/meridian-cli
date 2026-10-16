"""Process backend selection from resolved terminal policy and platform capability."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.launch_types import TerminalSurfaceMode

from .native_launcher import NativeProcessLauncher
from .ports import ProcessLauncher
from .pty_launcher import PtyProcessLauncher, can_use_pty
from .subprocess_launcher import SubprocessProcessLauncher
from .windows_launcher import WindowsConsoleLauncher, can_use_windows_console_launcher


def select_process_launcher(
    output_log_path: Path | None,
    *,
    terminal_surface_mode: TerminalSurfaceMode,
) -> ProcessLauncher:
    if output_log_path is not None:
        return PtyProcessLauncher() if can_use_pty() else SubprocessProcessLauncher()
    if can_use_windows_console_launcher():
        return WindowsConsoleLauncher()
    if terminal_surface_mode is TerminalSurfaceMode.NATIVE_INHERIT:
        return NativeProcessLauncher()
    return PtyProcessLauncher() if can_use_pty() else SubprocessProcessLauncher()
