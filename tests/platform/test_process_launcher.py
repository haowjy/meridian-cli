from __future__ import annotations

from typing import TYPE_CHECKING

from meridian.lib.launch.process.ports import (
    ProcessBackendId,
    ProcessSurfaceMode,
)
from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
from meridian.lib.launch.process.runner import (
    select_process_backend,
    select_process_launcher,
)
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher

if TYPE_CHECKING:
    import pytest


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

    selected = select_process_backend(None)
    assert isinstance(selected.launcher, PtyProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.PTY
    assert selected.contract.surface_mode is ProcessSurfaceMode.PTY_MEDIATED
    assert selected.contract.platform_family == "posix"


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

    selected = select_process_backend(None)
    assert isinstance(selected.launcher, SubprocessProcessLauncher)
    assert selected.contract.backend_id is ProcessBackendId.SUBPROCESS
    assert selected.contract.surface_mode is ProcessSurfaceMode.NATIVE_INHERIT
    assert selected.contract.platform_family == "portable"
