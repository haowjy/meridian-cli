from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
from meridian.lib.launch.process.ports import ProcessBackendId, ProcessBackendSurface
from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
from meridian.lib.launch.process.selector import select_process_backend
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher


@pytest.mark.parametrize("harness_id", [HarnessId.CODEX, HarnessId.OPENCODE])
def test_managed_native_harnesses_select_native_terminal(
    monkeypatch: pytest.MonkeyPatch,
    harness_id: HarnessId,
) -> None:
    mode = (
        get_default_harness_registry()
        .get_contract(harness_id)
        .capabilities.default_terminal_surface_mode
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_windows_console_launcher",
        lambda: False,
    )

    selected = select_process_backend(None, terminal_surface_mode=mode)
    assert isinstance(selected.launcher, NativeProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.NATIVE
    assert selected.contract.surface is ProcessBackendSurface.NATIVE_INHERIT
    assert selected.contract.platform_family == "posix"
    assert selected.contract.captures_output_to_artifact is False


@pytest.mark.parametrize("harness_id", [HarnessId.CLAUDE, HarnessId.PI])
def test_managed_pty_harnesses_select_pty(
    monkeypatch: pytest.MonkeyPatch,
    harness_id: HarnessId,
) -> None:
    mode = (
        get_default_harness_registry()
        .get_contract(harness_id)
        .capabilities.default_terminal_surface_mode
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_windows_console_launcher",
        lambda: False,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_pty",
        lambda: True,
    )

    selected = select_process_backend(None, terminal_surface_mode=mode)
    assert isinstance(selected.launcher, PtyProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.PTY
    assert selected.contract.surface is ProcessBackendSurface.PTY_MEDIATED
    assert selected.contract.platform_family == "posix"


@pytest.mark.parametrize(
    ("pty_available", "launcher_type", "surface"),
    [
        (True, PtyProcessLauncher, ProcessBackendSurface.PTY_MEDIATED),
        (False, SubprocessProcessLauncher, ProcessBackendSurface.PIPE_CAPTURE),
    ],
)
def test_capture_path_remains_pty_with_pipe_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pty_available: bool,
    launcher_type: type[object],
    surface: ProcessBackendSurface,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_pty",
        lambda: pty_available,
    )

    selected = select_process_backend(
        tmp_path / "output.jsonl",
        terminal_surface_mode=TerminalSurfaceMode.NATIVE_INHERIT,
    )
    assert isinstance(selected.launcher, launcher_type)
    assert selected.contract.surface is surface
    assert selected.contract.captures_output_to_artifact is True


def test_native_policy_uses_native_launcher_without_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_windows_console_launcher",
        lambda: False,
    )
    monkeypatch.setattr("meridian.lib.launch.process.selector.can_use_pty", lambda: False)

    selected = select_process_backend(
        None,
        terminal_surface_mode=TerminalSurfaceMode.NATIVE_INHERIT,
    )
    assert isinstance(selected.launcher, NativeProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.NATIVE
    assert selected.contract.surface is ProcessBackendSurface.NATIVE_INHERIT
    assert selected.contract.platform_family == "posix"
