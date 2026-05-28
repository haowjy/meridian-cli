# qa-validated: test-suite-redesign
"""Codex managed-path launch, projection manifest, and failure tests.

Verifies that Codex primary routes through the managed attach path,
that the system-field projection manifest is written correctly,
that managed marks running before attach returns, that Codex managed
failure raises an error with no fallback, and that fresh Codex
(no existing session) also uses the managed path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process import runner as process_runner
from meridian.lib.launch.process.ports import ProcessLauncher
from meridian.lib.launch.process.primary_attach import (
    PrimaryAttachError,
    PrimaryAttachOutcome,
)
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.state import session_store
from meridian.lib.state.spawn_store import get_spawn, list_spawns
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
        model="gpt-5.4",
        harness=HarnessId.CODEX,
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
            config_root=project_root.as_posix(),
            control_root=project_root.as_posix(),
            requested_task_cwd=resolved_execution_cwd.as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    return launch_context, harness_registry


@pytest.mark.slow
def test_run_harness_process_writes_codex_system_field_primary_projection_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    def fake_launcher_for(captured: dict[str, object]):
        def fake_run_primary_attach(
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
            _ = harness_id, spawn_id, control_root, task_cwd, env, spec, process_launcher
            captured["log_dir"] = Path(spawn_dir)
            return PrimaryAttachOutcome(exit_code=0, session_id=None, tui_pid=333)

        return fake_run_primary_attach

    harness_id = HarnessId.CODEX
    project_root = tmp_path / harness_id.value
    project_root.mkdir()
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    launch_context = build_launch_context(
        spawn_id=f"dry-run-primary-{harness_id.value}",
        request=SpawnRequest(
            prompt=f"{harness_id.value} primary prompt",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness=harness_id.value,
            extra_args=(f"--append-system-prompt={harness_id.value} passthrough system prompt",),
            session=SessionRequest(
                requested_harness_session_id="existing-codex-session",
                continue_chat_id="c-codex",
                primary_session_mode=SessionMode.RESUME.value,
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    adapter = harness_registry.get_subprocess_harness(harness_id)
    monkeypatch.setattr(adapter, "observe_session_id", lambda **kwargs: None)

    captured: dict[str, object] = {}
    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_launcher_for(captured),
        run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("managed primary path should avoid black-box launcher")
        ),
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    log_dir = captured["log_dir"]
    assert isinstance(log_dir, Path)
    system_prompt = (log_dir / "system-prompt.md").read_text(encoding="utf-8")
    assert not (log_dir / "prompt.md").exists()
    starting_prompt = (log_dir / "starting-prompt.md").read_text(encoding="utf-8")
    assert f"{harness_id.value} passthrough system prompt" in system_prompt
    assert f"{harness_id.value} passthrough system prompt" not in starting_prompt
    assert f"{harness_id.value} primary prompt" in starting_prompt
    assert json.loads((log_dir / "projection-manifest.json").read_text(encoding="utf-8")) == {
        "harness": harness_id.value,
        "surface": "primary",
        "channels": {
            "system_instruction": "system-field",
            "user_task_prompt": "user-turn",
            "task_context": "user-turn",
        },
    }
    assert outcome.exit_code == 0


@pytest.mark.slow
def test_run_harness_process_codex_primary_routes_to_managed_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-managed"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
        session=SessionRequest(
            requested_harness_session_id="existing-codex-session",
            continue_chat_id="c-codex",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    captured: dict[str, object] = {}
    selector_args: list[Path | None] = []

    def fake_select_process_launcher(output_log_path: Path | None) -> ProcessLauncher:
        selector_args.append(output_log_path)
        return SubprocessProcessLauncher()

    def fake_run_primary_attach(
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
        _ = spawn_id, control_root, task_cwd, env, spec, process_launcher, on_running
        captured["harness_id"] = harness_id
        spawn_dir = Path(spawn_dir)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        captured["spawn_dir"] = spawn_dir
        return PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5150)

    def fail_black_box(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        raise AssertionError("codex primary should use managed launcher path")

    monkeypatch.setattr(process_runner, "select_process_launcher", fake_select_process_launcher)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        run_primary_process_with_capture_fn=fail_black_box,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert captured["harness_id"] == HarnessId.CODEX
    assert isinstance(captured["spawn_dir"], Path)
    assert selector_args == [None]
    assert outcome.primary_spawn_id is not None
    assert list(launch_context.runtime_root.rglob("tui.log")) == []
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "thread-managed"


@pytest.mark.slow
def test_run_harness_process_codex_managed_attach_uses_control_root_with_distinct_task_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-managed-split-root"
    project_root.mkdir(parents=True)
    task_cwd = project_root / ".meridian" / "spawns" / "p-parent"
    task_cwd.mkdir(parents=True)
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
        execution_cwd=task_cwd,
        session=SessionRequest(
            requested_harness_session_id="existing-codex-session",
            continue_chat_id="c-codex",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    captured: dict[str, object] = {}

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        control_root: Any,
        passthrough_task_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        _ = spawn_id, spawn_dir, spec, process_launcher
        captured["harness_id"] = harness_id
        captured["control_root"] = control_root
        captured["task_cwd"] = passthrough_task_cwd
        captured["task_env"] = dict(env).get("MERIDIAN_TASK_CWD")
        if callable(on_running):
            on_running(5152)
        return PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5152)

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)
    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("managed primary path should avoid black-box launcher")
        ),
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert captured["harness_id"] == HarnessId.CODEX
    assert captured["control_root"] == project_root
    assert captured["task_cwd"] == task_cwd
    assert captured["task_env"] == task_cwd.as_posix()
    assert "MERIDIAN_TASK_DIR" in (launch_context.binding.run_params.appended_system_prompt or "")
    assert outcome.exit_code == 0
    assert outcome.primary_spawn_id is not None
    spawn_row = get_spawn(launch_context.runtime_root, outcome.primary_spawn_id)
    assert spawn_row is not None
    assert spawn_row.control_root == project_root.as_posix()
    assert spawn_row.task_cwd == task_cwd.as_posix()
    assert outcome.chat_id is not None
    session_row = session_store.get_session_record(launch_context.runtime_root, outcome.chat_id)
    assert session_row is not None
    assert session_row.control_root == project_root.as_posix()
    assert session_row.task_cwd == task_cwd.as_posix()


@pytest.mark.slow
def test_run_harness_process_managed_marks_running_before_attach_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-managed-running"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
        session=SessionRequest(
            requested_harness_session_id="existing-codex-session",
            continue_chat_id="c-codex",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    captured: dict[str, object] = {}

    def fake_run_primary_attach(
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
        _ = harness_id, spawn_id, spawn_dir, control_root, task_cwd, env, spec, process_launcher
        assert callable(on_running)
        assert list_spawns(launch_context.runtime_root)[0].status == "queued"
        on_running(5151)
        running_record = list_spawns(launch_context.runtime_root)[0]
        captured["status_seen_before_return"] = running_record.status
        captured["worker_pid_seen_before_return"] = running_record.worker_pid
        return PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5151)

    def fail_black_box(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        raise AssertionError("codex primary should use managed launcher path")

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        run_primary_process_with_capture_fn=fail_black_box,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert captured["status_seen_before_return"] == "running"
    assert captured["worker_pid_seen_before_return"] == 5151
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "thread-managed"


@pytest.mark.slow
def test_run_harness_process_codex_managed_failure_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex primary must use managed backend; failure raises error, no fallback."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-no-fallback"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
        session=SessionRequest(
            requested_harness_session_id="existing-codex-session",
            continue_chat_id="c-codex",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)

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
        Path(spawn_dir).mkdir(parents=True, exist_ok=True)
        raise PrimaryAttachError("managed startup error")

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    with pytest.raises(PrimaryAttachError, match="managed startup error"):
        run_harness_process(
            launch_context,
            harness_registry,
            run_primary_attach_fn=failing_managed,
            run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
                AssertionError("codex should not fall back to black-box")
            ),
            stop_session_fn=lambda *args, **kwargs: None,
            update_session_harness_id_fn=lambda *args, **kwargs: None,
        )
