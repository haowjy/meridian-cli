# qa-validated: pi-rpc-quiescence
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.launch.process.ports import (
    PRIMARY_STDERR_LOG_PATH_ENV,
    LaunchedProcess,
    ProcessLauncher,
)
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.history import iter_history_events
from meridian.lib.state.primary_meta import read_primary_metadata

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
        child_env={"MERIDIAN_PI_SESSION_ROLE": "primary"},
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
    expected_env = {"MERIDIAN_PI_SESSION_ROLE": "primary"}
    expected_env[PRIMARY_STDERR_LOG_PATH_ENV] = str((tmp_path / "logs") / "stderr.log")
    assert capture_calls == [
        (("pi",), tmp_path, expected_env, None)
    ]


def test_persist_pi_primary_stderr_diagnostics_records_lifecycle_and_parse_errors(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-pi-primary-stderr-diag")
    log_dir = tmp_path / "spawns" / str(spawn_id)
    log_dir.mkdir(parents=True)
    (log_dir / "stderr.log").write_text(
        "\n".join(
            (
                '{"type":"meridian.subspawn.start","schema_version":1,'
                '"parent_spawn_id":"p-pi-primary-stderr-diag","correlation_id":"j-1",'
                '"subspawn_id":"j-1","emitted_at_ms":1760000000000}',
                '{"type":"meridian.notification.queued","schema_version":1,'
                '"parent_spawn_id":"p-pi-primary-stderr-diag","correlation_id":"n-1",'
                '"emitted_at_ms":1760000000001}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    runner_module._persist_pi_primary_stderr_diagnostics(
        spawn_id=spawn_id,
        log_dir=log_dir,
    )

    history = list(iter_history_events(log_dir / "history.jsonl"))
    assert [event["event_type"] for event in history] == [
        "meridian.subspawn.start",
        "meridian.lifecycle.parse_error",
    ]
    assert history[1]["payload"]["reason"] == "missing_notification_id"


def test_finalize_lifecycle_observes_session_using_runtime_child_cwd(tmp_path: Path) -> None:
    captured_observe_kwargs: dict[str, object] = {}

    class _Adapter:
        def observe_session_id(self, **kwargs: object) -> None:
            captured_observe_kwargs.update(kwargs)
            return None

    control_root = tmp_path / "control-root"
    launch_child_cwd = control_root / "nested"
    launch_child_cwd.mkdir(parents=True)
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")

    exit_code, resolved_session_id = runner_module._finalize_lifecycle_and_observe_session(
        primary_spawn_id=None,
        exit_code=0,
        resolved_harness_session_id="ses-current",
        initial_persisted_harness_session_id="ses-current",
        harness_adapter=_Adapter(),
        artifacts=artifacts,
        project_root=control_root,
        launch_child_cwd=launch_child_cwd,
        model_id=None,
        runtime_root=tmp_path,
        primary_started=0.0,
        primary_started_epoch=time.time(),
        primary_started_local_iso=None,
        managed=_ManagedNoop(),
        spawn_service=None,  # type: ignore[arg-type]
    )

    assert exit_code == 0
    assert resolved_session_id == "ses-current"
    assert captured_observe_kwargs["project_root"] == launch_child_cwd


def test_finalize_lifecycle_can_skip_adapter_session_observation(tmp_path: Path) -> None:
    class _Adapter:
        def observe_session_id(self, **_kwargs: object) -> None:
            raise AssertionError("observe_session_id should be skipped")

    control_root = tmp_path / "control-root"
    control_root.mkdir(parents=True)
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")

    exit_code, resolved_session_id = runner_module._finalize_lifecycle_and_observe_session(
        primary_spawn_id=None,
        exit_code=0,
        resolved_harness_session_id="ses-preserved",
        initial_persisted_harness_session_id="ses-preserved",
        harness_adapter=_Adapter(),
        artifacts=artifacts,
        project_root=control_root,
        launch_child_cwd=control_root,
        model_id=None,
        runtime_root=tmp_path,
        primary_started=0.0,
        primary_started_epoch=time.time(),
        primary_started_local_iso=None,
        managed=_ManagedNoop(),
        spawn_service=None,  # type: ignore[arg-type]
        observe_adapter_session_id=False,
    )

    assert exit_code == 0
    assert resolved_session_id == "ses-preserved"


def test_execute_primary_process_for_pi_uses_default_launcher_selection_when_pty_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runner_module, "can_use_pty", lambda: True)
    monkeypatch.setattr(runner_module, "can_use_windows_console_launcher", lambda: False)
    pty_launch_envs: list[dict[str, str]] = []

    def _fake_pty_launch(
        _self: ProcessLauncher,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: Callable[[int], None] | None = None,
    ) -> LaunchedProcess:
        _ = command, cwd
        pty_launch_envs.append(dict(env))
        assert output_log_path is None
        if on_child_started is not None:
            on_child_started(909)
        return LaunchedProcess(exit_code=0, pid=909)

    monkeypatch.setattr(
        "meridian.lib.launch.process.runner.PtyProcessLauncher.launch",
        _fake_pty_launch,
    )

    on_running_pids: list[int] = []
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    exit_code, managed_session_id = runner_module._execute_primary_process(
        harness_id=HarnessId.PI,
        primary_spawn_id=SpawnId("p-pi-primary-force-subprocess"),
        log_dir=log_dir,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        command=("pi",),
        harness_contract=PiAdapter().contract,
        managed=_ManagedNoop(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=runner_module.run_primary_process_with_capture,
        run_primary_attach_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("managed attach should not run")
        ),
        on_running=on_running_pids.append,
    )

    assert exit_code == 0
    assert managed_session_id is None
    assert on_running_pids == [909]
    assert pty_launch_envs
    assert pty_launch_envs[0][PRIMARY_STDERR_LOG_PATH_ENV] == str(log_dir / "stderr.log")
    assert not (log_dir / "stderr.log").exists()


def test_pi_primary_real_subprocess_stderr_is_projected_to_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runner_module, "can_use_pty", lambda: False)
    monkeypatch.setattr(runner_module, "can_use_windows_console_launcher", lambda: False)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    spawn_id = SpawnId("p-pi-primary-real-stderr")

    exit_code, managed_session_id = runner_module._execute_primary_process(
        harness_id=HarnessId.PI,
        primary_spawn_id=spawn_id,
        log_dir=log_dir,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        command=(
            sys.executable,
            "-c",
            (
                "import json,sys;"
                "sys.stderr.write(json.dumps({"
                "'type':'meridian.subspawn.start',"
                "'schema_version':1,"
                "'parent_spawn_id':'p-pi-primary-real-stderr',"
                "'correlation_id':'j-1',"
                "'subspawn_id':'j-1',"
                "'emitted_at_ms':1760000000000"
                "})+'\\n');"
                "sys.stderr.flush()"
            ),
        ),
        harness_contract=PiAdapter().contract,
        managed=_ManagedNoop(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=runner_module.run_primary_process_with_capture,
        run_primary_attach_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("managed attach should not run")
        ),
        on_running=lambda _pid: None,
    )

    assert exit_code == 0
    assert managed_session_id is None
    runner_module._persist_pi_primary_stderr_diagnostics(
        spawn_id=spawn_id,
        log_dir=log_dir,
    )
    history = list(iter_history_events(log_dir / "history.jsonl"))
    assert [event["event_type"] for event in history] == ["meridian.subspawn.start"]


def test_write_native_primary_metadata_redacts_secret_argv(tmp_path: Path) -> None:
    spawn_id = "p-pi-primary-redacted-meta"
    spawn_dir = tmp_path / "spawns" / spawn_id
    spawn_dir.mkdir(parents=True)

    runner_module._write_native_primary_metadata(
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
