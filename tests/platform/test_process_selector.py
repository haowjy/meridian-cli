from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
from meridian.lib.launch.process.ports import LaunchedProcess, ProcessBackendId
from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
from meridian.lib.launch.process.selector import (
    select_process_backend,
    select_process_launcher,
)
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.process.windows_launcher import WindowsConsoleLauncher


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
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_native_terminal",
        lambda: True,
    )

    selected = select_process_backend(None, terminal_surface_mode=mode)
    assert isinstance(selected.launcher, NativeProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.NATIVE
    assert selected.contract.surface_mode is TerminalSurfaceMode.NATIVE_INHERIT
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
    assert selected.contract.surface_mode is TerminalSurfaceMode.PTY_MEDIATED
    assert selected.contract.platform_family == "posix"


@pytest.mark.parametrize(
    ("pty_available", "launcher_type", "surface_mode"),
    [
        (True, PtyProcessLauncher, TerminalSurfaceMode.PTY_MEDIATED),
        (False, SubprocessProcessLauncher, TerminalSurfaceMode.PIPE_CAPTURE),
    ],
)
def test_capture_path_remains_pty_with_pipe_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pty_available: bool,
    launcher_type: type[object],
    surface_mode: TerminalSurfaceMode,
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
    assert selected.contract.surface_mode is surface_mode
    assert selected.contract.captures_output_to_artifact is True


@pytest.mark.parametrize(
    "mode",
    [TerminalSurfaceMode.NATIVE_INHERIT, TerminalSurfaceMode.PTY_MEDIATED],
)
def test_no_tty_uses_portable_inherit_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mode: TerminalSurfaceMode,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_windows_console_launcher",
        lambda: False,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_native_terminal",
        lambda: False,
    )
    monkeypatch.setattr("meridian.lib.launch.process.selector.can_use_pty", lambda: False)

    selected = select_process_backend(None, terminal_surface_mode=mode)
    assert isinstance(selected.launcher, SubprocessProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.SUBPROCESS
    assert selected.contract.surface_mode is TerminalSurfaceMode.NATIVE_INHERIT
    assert selected.contract.platform_family == "portable"


def test_native_mode_uses_windows_console_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.selector.can_use_windows_console_launcher",
        lambda: True,
    )

    launcher = select_process_launcher(
        None,
        terminal_surface_mode=TerminalSurfaceMode.NATIVE_INHERIT,
    )

    assert isinstance(launcher, WindowsConsoleLauncher)


def test_windows_console_launcher_forces_console_inheritance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    expected = LaunchedProcess(exit_code=12, pid=456)

    class _Running:
        pid = 456

        def wait(self) -> LaunchedProcess:
            return expected

        def terminate(self) -> None:
            return None

        def cancel_wait(self) -> None:
            return None

    class FakeSubprocessProcessLauncher:
        def start(
            self,
            *,
            command: tuple[str, ...],
            cwd: Path,
            env: dict[str, str],
            output_log_path: Path | None,
        ) -> _Running:
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            captured["output_log_path"] = output_log_path
            return _Running()

    monkeypatch.setattr(
        "meridian.lib.launch.process.windows_launcher.SubprocessProcessLauncher",
        FakeSubprocessProcessLauncher,
    )
    running = WindowsConsoleLauncher().start(
        command=("meridian-harness", "--run"),
        cwd=tmp_path,
        env={"MERIDIAN_TEST": "1"},
        output_log_path=tmp_path / "history.jsonl",
    )

    assert captured["output_log_path"] == tmp_path / "history.jsonl"
    assert running.pid == 456
    assert running.wait() == expected
