# qa-validated: pi-rpc-quiescence
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.launch.process.ports import ProcessLauncher
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.artifact_store import LocalStore

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
        task_cwd=None,
        child_env={"MERIDIAN_PI_SESSION_ROLE": "primary"},
        launch_spec=_build_spec(),
        command=("meridian-pi",),
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
    assert capture_calls == [
        (("meridian-pi",), tmp_path, {"MERIDIAN_PI_SESSION_ROLE": "primary"}, None)
    ]


def test_execute_primary_process_for_pi_does_not_capture_output_jsonl(tmp_path: Path) -> None:
    output_log_paths: list[Path | None] = []

    def _fake_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        _env: dict[str, str],
        output_log_path: Path | None,
        _on_child_started: Callable[[int], None] | None,
    ) -> tuple[int, int | None]:
        output_log_paths.append(output_log_path)
        return 0, None

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
        raise AssertionError("managed attach should not run")

    exit_code, managed_session_id = runner_module._execute_primary_process(
        harness_id=HarnessId.PI,
        primary_spawn_id=SpawnId("p-pi-primary-no-capture"),
        log_dir=tmp_path / "logs",
        control_root=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        command=("meridian-pi",),
        harness_contract=PiAdapter().contract,
        managed=_ManagedNoop(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=_fake_capture,
        run_primary_attach_fn=_unexpected_attach,
        on_running=lambda _pid: None,
    )

    assert exit_code == 0
    assert managed_session_id is None
    assert output_log_paths == [None]


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
