# qa-validated: pi-rpc-quiescence
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.launch.process.ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    ProcessLauncher,
)
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.primary_meta import read_primary_metadata
from meridian.lib.state.spawn_store import start_spawn

if TYPE_CHECKING:
    import pytest


class _ManagedNoop:
    def record_harness_session_id(self, session_id: str) -> None:
        _ = session_id


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def test_execute_primary_process_for_pi_uses_native_blackbox_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture_calls: list[tuple[tuple[str, ...], Path, dict[str, str], Path | None]] = []
    on_running_pids: list[int] = []

    def _fake_capture(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: Callable[[int], None] | None,
    ) -> tuple[int, int | None]:
        capture_calls.append((command, cwd, env, output_log_path))
        if on_child_started is not None:
            on_child_started(4242)
        return 0, 4242

    def _unexpected_attach(
        _harness_id: HarnessId,
        _spawn_id: SpawnId,
        _spawn_dir: Path,
        _control_root: Path,
        _task_cwd: Path | None,
        _env: dict[str, str],
        _spec: ResolvedLaunchSpec,
        _process_launcher: ProcessLauncher,
        _on_running: Callable[[int], None] | None = None,
    ) -> PrimaryAttachOutcome:
        raise AssertionError("Pi primary must not route through managed primary attach")

    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": (_ for _ in ()).throw(AssertionError("input() should not be called")),
    )

    exit_code, managed_session_id = runner_module._execute_primary_process(
        harness_id=HarnessId.PI,
        primary_spawn_id=SpawnId("p-pi-primary-native"),
        log_dir=tmp_path / "logs",
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={"_MERIDIAN_PI_SESSION_ROLE": "primary"},
        launch_spec=_build_spec(),
        command=("pi",),
        harness_contract=PiAdapter().contract,
        managed=_ManagedNoop(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=_fake_capture,
        run_primary_attach_fn=_unexpected_attach,
        on_running=on_running_pids.append,
    )

    assert exit_code == 0
    assert managed_session_id is None
    assert on_running_pids == [4242]
    expected_env = {"_MERIDIAN_PI_SESSION_ROLE": "primary"}
    expected_env[PRIMARY_STDERR_LOG_PATH_ENV] = str((tmp_path / "logs") / "stderr.log")
    assert capture_calls == [(("pi",), tmp_path, expected_env, None)]


def test_write_native_primary_metadata_redacts_secret_argv(tmp_path: Path) -> None:
    spawn_id = "p-pi-primary-redacted-meta"
    spawn_dir = tmp_path / "spawns" / spawn_id
    start_spawn(
        tmp_path,
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="pi",
        prompt="test",
        spawn_id=spawn_id,
        status="running",
    )

    runner_module._write_native_primary_metadata(
        runtime_root=tmp_path,
        spawn_id=SpawnId(spawn_id),
        spawn_dir=spawn_dir,
        command=(
            "/usr/local/bin/pi",
            "--profile",
            "safe",
            "--api-key",
            "super-secret",
            "--auth-token=secret-token",
        ),
        launch_cwd=tmp_path,
        launcher_pid=100,
        tui_pid=200,
        activity="idle",
        started_at_epoch=1.0,
        ended_at_epoch=2.0,
        exit_code=0,
        harness_session_id="ses-1",
    )

    metadata = read_primary_metadata(tmp_path, spawn_id)
    assert metadata is not None
    assert metadata.command is not None
    assert metadata.command[0] == "/usr/local/bin/pi"
    assert "--profile" in metadata.command
    assert metadata.command[metadata.command.index("--profile") + 1] == "safe"
    assert "--api-key" in metadata.command
    assert metadata.command[metadata.command.index("--api-key") + 1] == "<redacted>"
    assert "--auth-token=<redacted>" in metadata.command
    assert "super-secret" not in metadata.command
    assert "--auth-token=secret-token" not in metadata.command
