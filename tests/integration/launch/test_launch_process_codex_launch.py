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
from meridian.lib.launch import process
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process import runner as process_runner
from meridian.lib.launch.process.ports import ProcessLauncher
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.state.spawn_store import list_spawns


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_primary_launch_context(
    *,
    project_root: Path,
    harness_id: HarnessId,
    model: str,
    prompt: str = "primary prompt",
    extra_args: tuple[str, ...] = (),
    session: SessionRequest | None = None,
) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
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
            project_paths_execution_cwd=project_root.as_posix(),
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
            execution_cwd: Any,
            env: Any,
            spec: Any,
            process_launcher: Any,
            on_running: Any = None,
        ) -> process.PrimaryAttachOutcome:
            captured["log_dir"] = Path(spawn_dir)
            return process.PrimaryAttachOutcome(exit_code=0, session_id=None, tui_pid=333)

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
            extra_args=(
                f"--append-system-prompt={harness_id.value} passthrough system prompt",
            ),
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
    outcome = process.run_harness_process(
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
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> process.PrimaryAttachOutcome:
        captured["harness_id"] = harness_id
        spawn_dir = Path(spawn_dir)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        captured["spawn_dir"] = spawn_dir
        return process.PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5150)

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

    outcome = process.run_harness_process(
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
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> process.PrimaryAttachOutcome:
        assert callable(on_running)
        assert list_spawns(launch_context.runtime_root)[0].status == "queued"
        on_running(5151)
        running_record = list_spawns(launch_context.runtime_root)[0]
        captured["status_seen_before_return"] = running_record.status
        captured["worker_pid_seen_before_return"] = running_record.worker_pid
        return process.PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5151)

    def fail_black_box(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        raise AssertionError("codex primary should use managed launcher path")

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = process.run_harness_process(
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
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> process.PrimaryAttachOutcome:
        Path(spawn_dir).mkdir(parents=True, exist_ok=True)
        raise process.PrimaryAttachError("managed startup error")

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    with pytest.raises(process.PrimaryAttachError, match="managed startup error"):
        process.run_harness_process(
            launch_context,
            harness_registry,
            run_primary_attach_fn=failing_managed,
            run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
                AssertionError("codex should not fall back to black-box")
            ),
            stop_session_fn=lambda *args, **kwargs: None,
            update_session_harness_id_fn=lambda *args, **kwargs: None,
        )


@pytest.mark.slow
def test_run_harness_process_fresh_codex_primary_routes_to_managed_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh Codex primary (no session to resume) uses managed attach, not black-box."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-fresh-managed"
    project_root.mkdir()
    # No session request - this is a fresh primary
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
        prompt="fresh codex primary prompt",
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    managed_calls = 0

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> process.PrimaryAttachOutcome:
        nonlocal managed_calls
        managed_calls += 1
        # Verify this is for Codex
        assert harness_id == HarnessId.CODEX
        # Call on_running to mark spawn as running
        if callable(on_running):
            on_running(12345)
        return process.PrimaryAttachOutcome(
            exit_code=0, session_id="fresh-thread-id", tui_pid=12345
        )

    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = process.run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("fresh codex primary should use managed path, not black-box")
        ),
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert managed_calls == 1
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "fresh-thread-id"
