"""Process backend selection from resolved terminal policy and platform capability."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.launch_types import TerminalSurfaceMode

from .native_launcher import NativeProcessLauncher
from .ports import (
    ProcessBackendId,
    ProcessBackendSurface,
    ProcessLauncher,
    ProcessPlatformContract,
    SelectedProcessLauncher,
)
from .pty_launcher import PtyProcessLauncher, can_use_pty
from .subprocess_launcher import SubprocessProcessLauncher
from .windows_launcher import WindowsConsoleLauncher, can_use_windows_console_launcher


def select_process_launcher(
    output_log_path: Path | None,
    *,
    terminal_surface_mode: TerminalSurfaceMode,
) -> ProcessLauncher:
    """Choose one launcher for an explicitly resolved terminal surface."""

    return select_process_backend(
        output_log_path,
        terminal_surface_mode=terminal_surface_mode,
    ).launcher


def select_process_backend(
    output_log_path: Path | None,
    *,
    terminal_surface_mode: TerminalSurfaceMode,
) -> SelectedProcessLauncher:
    """Choose the launcher and describe the actual IO surface it provides."""

    if output_log_path is not None:
        if can_use_pty():
            return SelectedProcessLauncher(
                launcher=PtyProcessLauncher(),
                contract=ProcessPlatformContract(
                    backend_id=ProcessBackendId.PTY,
                    surface=ProcessBackendSurface.PTY_MEDIATED,
                    captures_output_to_artifact=True,
                    platform_family="posix",
                ),
            )
        return SelectedProcessLauncher(
            launcher=SubprocessProcessLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.SUBPROCESS,
                surface=ProcessBackendSurface.PIPE_CAPTURE,
                captures_output_to_artifact=True,
                platform_family="portable",
            ),
        )

    if terminal_surface_mode is TerminalSurfaceMode.NATIVE_INHERIT:
        if can_use_windows_console_launcher():
            return SelectedProcessLauncher(
                launcher=WindowsConsoleLauncher(),
                contract=ProcessPlatformContract(
                    backend_id=ProcessBackendId.WINDOWS_CONSOLE,
                    surface=ProcessBackendSurface.NATIVE_INHERIT,
                    captures_output_to_artifact=False,
                    platform_family="windows",
                ),
            )
        return SelectedProcessLauncher(
            launcher=NativeProcessLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.NATIVE,
                surface=ProcessBackendSurface.NATIVE_INHERIT,
                captures_output_to_artifact=False,
                platform_family="posix",
            ),
        )

    if terminal_surface_mode is TerminalSurfaceMode.PTY_MEDIATED and can_use_pty():
        return SelectedProcessLauncher(
            launcher=PtyProcessLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.PTY,
                surface=ProcessBackendSurface.PTY_MEDIATED,
                captures_output_to_artifact=False,
                platform_family="posix",
            ),
        )

    if can_use_windows_console_launcher():
        return SelectedProcessLauncher(
            launcher=WindowsConsoleLauncher(),
            contract=ProcessPlatformContract(
                backend_id=ProcessBackendId.WINDOWS_CONSOLE,
                surface=ProcessBackendSurface.NATIVE_INHERIT,
                captures_output_to_artifact=False,
                platform_family="windows",
            ),
        )
    return SelectedProcessLauncher(
        launcher=SubprocessProcessLauncher(),
        contract=ProcessPlatformContract(
            backend_id=ProcessBackendId.SUBPROCESS,
            surface=ProcessBackendSurface.NATIVE_INHERIT,
            captures_output_to_artifact=False,
            platform_family="portable",
        ),
    )


__all__ = ["select_process_backend", "select_process_launcher"]
