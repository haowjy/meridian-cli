# qa-validated: test-suite-redesign
"""OpenCode-specific launch tests and primary dry-run test.

Verifies OpenCode projection manifest (system-field), managed path
routing (resume and fork modes), fallback to black-box when managed
backend fails, and the top-level launch_primary dry-run contract.
"""

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
        model="gemini-2.5-pro",
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






@pytest.mark.slow
def test_run_harness_process_managed_failure_falls_back_to_black_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenCode can fall back to black-box when managed backend fails."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "opencode-fallback"
    project_root.mkdir()
    task_cwd = project_root / ".meridian" / "spawns" / "p-parent"
    task_cwd.mkdir(parents=True)
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.OPENCODE,
        model="gemini-2.5-pro",
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

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=failing_managed,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert managed_calls == 1
    assert black_box_calls == 1
    assert captured_spawn_dir is not None
    assert not (captured_spawn_dir / PRIMARY_META_FILENAME).exists()
    assert not (captured_spawn_dir / OUTPUT_FILENAME).exists()
    assert captured_black_box_cwd == project_root
    assert list(launch_context.runtime_root.rglob("tui.log")) == []
    assert outcome.exit_code == 0


def test_opencode_streaming_logs_effort_warning_without_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    payload = project_opencode_spec_to_session_payload(
        ResolvedLaunchSpec(
            model="gemini-2.5-pro",
            effort="medium",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
    )

    assert payload["model"] == "gemini-2.5-pro"
    assert payload["modelID"] == "gemini-2.5-pro"
    assert "effort" not in payload
    assert (
        "OpenCode streaming does not support effort override; ignoring effort=medium"
        in caplog.text
    )
