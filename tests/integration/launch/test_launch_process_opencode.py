# qa-validated: test-suite-redesign
"""OpenCode projections, managed launch failures, and non-executing primary plans."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_spec_to_session_payload,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.constants import OUTPUT_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.launch.process.primary_attach import (
    PrimaryAttachError,
    PrimaryAttachOutcome,
)
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from tests.support.launch import stub_bundle_request_and_resolve


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="google/gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
    )


def _build_primary_launch_context(
    *,
    project_root: Path,
    harness_id: HarnessId,
    model: str,
    prompt: str = "primary prompt",
    extra_args: tuple[str, ...] = (),
    session: SessionRequest | None = None,
    execution_cwd: Path | None = None,
) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    resolved_execution_cwd = execution_cwd or project_root
    launch_context = build_launch_context(
        spawn_id=f"dry-run-primary-{harness_id.value}",
        request=SpawnRequest(
            prompt=prompt,
            prompt_is_composed=False,
            model=model,
            harness=harness_id.value,
            extra_args=extra_args,
            session=session or SessionRequest(),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    return launch_context, harness_registry


def test_run_primary_attach_preserves_startup_failure_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailingPrimaryAttachLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, **_kwargs: object) -> object:
            raise TimeoutError("OpenCode session endpoint did not become ready within 12.0s")

    monkeypatch.setattr(runner_module, "PrimaryAttachLauncher", FailingPrimaryAttachLauncher)

    with pytest.raises(PrimaryAttachError) as exc_info:
        runner_module.run_primary_attach(
            harness_id=HarnessId.OPENCODE,
            spawn_id=runner_module.SpawnId("p-timeout-cause"),
            spawn_dir=tmp_path / "spawn",
            control_root=tmp_path,
            task_cwd=tmp_path,
            env={},
            spec=ResolvedLaunchSpec(
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            process_launcher=lambda **_kwargs: (1, None),
        )

    assert str(exc_info.value) == (
        "Managed primary attach failed for opencode: "
        "OpenCode session endpoint did not become ready within 12.0s"
    )
    assert isinstance(exc_info.value.__cause__, TimeoutError)


@pytest.mark.slow
def test_run_harness_process_managed_failure_does_not_fall_back_to_black_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Model/managed startup failures must not silently launch a different mode."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "opencode-fallback"
    project_root.mkdir()
    task_cwd = project_root / ".meridian" / "spawns" / "p-parent"
    task_cwd.mkdir(parents=True)
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.OPENCODE,
        model="google/gemini-2.5-pro",
        execution_cwd=task_cwd,
        session=SessionRequest(
            requested_harness_session_id="existing-opencode-session",
            continue_chat_id="c-opencode",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    opencode_adapter = harness_registry.get_subprocess_harness(HarnessId.OPENCODE)
    managed_calls = 0
    black_box_calls = 0
    captured_spawn_dir: Path | None = None
    captured_black_box_cwd: Path | None = None

    def failing_managed(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        control_root: Any,
        task_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        _ = harness_id, spawn_id, control_root, task_cwd, env, spec, process_launcher, on_running
        nonlocal managed_calls
        nonlocal captured_spawn_dir
        managed_calls += 1
        spawn_dir = Path(spawn_dir)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / PRIMARY_META_FILENAME).write_text(
            '{"managed_backend":true}\n',
            encoding="utf-8",
        )
        (spawn_dir / OUTPUT_FILENAME).write_text(
            '{"type":"turn/started"}\n',
            encoding="utf-8",
        )
        captured_spawn_dir = spawn_dir
        raise PrimaryAttachError("managed startup error")

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        nonlocal black_box_calls
        nonlocal captured_black_box_cwd
        black_box_calls += 1
        captured_black_box_cwd = Path(cwd)
        assert output_log_path is None
        assert callable(on_child_started)
        on_child_started(9494)
        return (0, 9494)

    monkeypatch.setattr(opencode_adapter, "observe_session_id", lambda **kwargs: None)

    with pytest.raises(PrimaryAttachError, match="managed startup error"):
        run_harness_process(
            launch_context,
            harness_registry,
            run_primary_attach_fn=failing_managed,
            run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
            stop_session_fn=lambda *args, **kwargs: None,
            update_session_harness_id_fn=lambda *args, **kwargs: None,
        )

    assert managed_calls == 1
    assert black_box_calls == 0
    assert captured_spawn_dir is not None
    assert captured_black_box_cwd is None
    assert list(launch_context.runtime_root.rglob("tui.log")) == []


def test_opencode_streaming_logs_effort_warning_without_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    payload = project_opencode_spec_to_session_payload(
        ResolvedLaunchSpec(
            model="google/gemini-2.5-pro",
            effort="medium",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    assert payload["model"] == {"id": "gemini-2.5-pro", "providerID": "google"}
    assert "modelID" not in payload
    assert "effort" not in payload
    assert (
        "OpenCode streaming does not support effort override; ignoring effort=medium" in caplog.text
    )


def test_managed_primary_dryrun_has_one_truthful_structured_plan(
    tmp_path: Path, monkeypatch
) -> None:
    from meridian.cli.primary_launch import PrimaryLaunchOutput
    from meridian.lib.launch import launch_primary
    from meridian.lib.launch.types import LaunchRequest

    requests = stub_bundle_request_and_resolve(
        monkeypatch, model="google/gemini-2.5-pro", harness=HarnessId.OPENCODE
    )
    _write_minimal_mars_config(tmp_path)
    result = launch_primary(
        project_root=tmp_path,
        request=LaunchRequest(model="google/gemini-2.5-pro", harness="opencode", dry_run=True),
        harness_registry=get_default_harness_registry(),
    )
    from meridian.lib.launch.bundle_adapter import _build_bundle_command

    assert requests and requests[0].no_refresh_models
    assert "--no-refresh-models" in _build_bundle_command(requests[0])
    assert result.command == ()
    plan = result.launch_plan
    assert plan is not None
    assert plan.backend_command[:2] == ("opencode", "serve")
    assert plan.attach_command[:2] == ("opencode", "attach")
    assert plan.bootstrap_payload["model"] == {"id": "gemini-2.5-pro", "providerID": "google"}
    assert plan.requested_model == plan.model == "google/gemini-2.5-pro"
    assert plan.native_observations == "unavailable (dry-run)"
    output = PrimaryLaunchOutput(
        message="dry-run", exit_code=0, command=result.command, launch_plan=plan
    )
    assert output.model_dump(mode="json")["command"] == []
    assert "opencode serve" in output.format_text()
    assert "GET /config/providers" in output.format_text()
    assert "Native configuration and actual message model are unavailable" in output.format_text()
