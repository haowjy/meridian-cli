from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.process.ports import LaunchedProcess
from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
from meridian.lib.launch.process.runner import select_process_launcher
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.process.windows_launcher import WindowsConsoleLauncher


@pytest.mark.unit
def test_select_process_launcher_uses_windows_console_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_windows_console_launcher",
        lambda: True,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_pty",
        lambda: True,
    )

    launcher = select_process_launcher(None)

    assert isinstance(launcher, WindowsConsoleLauncher)


@pytest.mark.unit
def test_select_process_launcher_with_capture_uses_subprocess_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_windows_console_launcher",
        lambda: True,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_pty",
        lambda: False,
    )

    launcher = select_process_launcher(tmp_path / "output.jsonl")

    assert isinstance(launcher, SubprocessProcessLauncher)


@pytest.mark.unit
def test_select_process_launcher_non_windows_uses_posix_pty_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_windows_console_launcher",
        lambda: False,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_pty",
        lambda: True,
    )

    launcher = select_process_launcher(None)

    assert isinstance(launcher, PtyProcessLauncher)


@pytest.mark.unit
def test_select_process_launcher_non_windows_falls_back_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_windows_console_launcher",
        lambda: False,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.can_use_pty",
        lambda: False,
    )

    launcher = select_process_launcher(None)

    assert isinstance(launcher, SubprocessProcessLauncher)


@pytest.mark.unit
def test_windows_console_launcher_forces_console_inheritance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    expected = LaunchedProcess(exit_code=12, pid=456)

    class FakeSubprocessProcessLauncher:
        def launch(
            self,
            *,
            command: tuple[str, ...],
            cwd: Path,
            env: dict[str, str],
            output_log_path: Path | None,
            on_child_started=None,
        ) -> LaunchedProcess:
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            captured["output_log_path"] = output_log_path
            if on_child_started is not None:
                on_child_started(456)
            return expected

    monkeypatch.setattr(
        "meridian.lib.launch.process.windows_launcher.SubprocessProcessLauncher",
        FakeSubprocessProcessLauncher,
    )
    child_started: list[int] = []

    result = WindowsConsoleLauncher().launch(
        command=("meridian-harness", "--run"),
        cwd=tmp_path,
        env={"MERIDIAN_TEST": "1"},
        output_log_path=tmp_path / "history.jsonl",
        on_child_started=child_started.append,
    )

    assert captured["output_log_path"] is None
    assert child_started == [456]
    assert result == expected
