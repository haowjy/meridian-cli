# qa-validated: test-suite-redesign
"""Claude-specific prompt projection and black-box launch path tests.

Verifies that Claude's prompt file is written before launch, that
black-box mode produces no TUI log artifact, that print-JSON mode
captures session ID from output, and that Claude stays on the
black-box path (not managed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.constants import OUTPUT_FILENAME
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.state.spawn_store import list_spawns
from tests.support.launch import assert_task_cwd_instruction, stub_bundle_request_and_resolve


def _write_minimal_mars_config(project_root: Path, *, claude_agent_copy: bool = False) -> None:
    agent_copy = (
        '\n[settings.meridian.agent_copy]\nharnesses = ["claude"]\n' if claude_agent_copy else ""
    )
    (project_root / "mars.toml").write_text(
        f'[settings]\ntargets = [".claude"]\n{agent_copy}',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
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
    claude_agent_copy: bool = False,
    agent_opt_out: bool = False,
) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root, claude_agent_copy=claude_agent_copy)
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
            agent_opt_out=agent_opt_out,
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
def test_run_harness_process_writes_prompt_file_before_primary_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    launch_context = build_launch_context(
        spawn_id="dry-run-primary",
        request=SpawnRequest(
            prompt="primary prompt",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness=HarnessId.CLAUDE.value,
            extra_args=("--append-system-prompt=passthrough system prompt",),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )

    captured: dict[str, object] = {}
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)

    def fake_run_primary_process_with_capture(
        command: tuple[str, ...],
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        command = tuple(command)
        captured["command"] = command
        captured["output_log_path"] = output_log_path
        prompt_flag_index = command.index("--append-system-prompt-file")
        prompt_file_path = Path(command[prompt_flag_index + 1])
        captured["prompt_file_exists"] = prompt_file_path.exists()
        captured["prompt_file_text"] = (
            prompt_file_path.read_text(encoding="utf-8") if prompt_file_path.exists() else None
        )
        captured["log_dir"] = prompt_file_path.parent
        assert callable(on_child_started)
        on_child_started(222)
        return (0, 222)

    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert captured["prompt_file_exists"] is True
    assert captured["output_log_path"] is None
    prompt_file_text = captured["prompt_file_text"]
    assert isinstance(prompt_file_text, str)
    # Phase 3A: system-prompt.md should contain only SYSTEM_INSTRUCTION
    # (passthrough fragments), not USER_TASK_PROMPT (primary prompt)
    assert "passthrough system prompt" in prompt_file_text
    # User task prompt should now be in the positional argument (user-turn channel)
    assert "primary prompt" not in prompt_file_text
    # Verify the command includes the positional prompt argument for Claude interactive
    command = captured.get("command")
    assert command is not None
    assert "primary prompt" in command[-1]  # Positional arg is last
    log_dir = captured["log_dir"]
    assert isinstance(log_dir, Path)
    starting_prompt = (log_dir / "starting-prompt.md").read_text(encoding="utf-8")
    assert "primary prompt" in starting_prompt
    assert "passthrough system prompt" not in starting_prompt
    assert not (log_dir / "prompt.md").exists()
    assert json.loads((log_dir / "projection-manifest.json").read_text(encoding="utf-8")) == {
        "harness": "claude",
        "surface": "primary",
        "channels": {
            "system_instruction": "append-system-prompt",
            "user_task_prompt": "user-turn",
            "task_context": "user-turn",
        },
    }
    assert outcome.exit_code == 0



@pytest.mark.slow
def test_run_harness_process_black_box_primary_uses_control_root_with_distinct_task_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "claude-split-root"
    project_root.mkdir(parents=True)
    task_cwd = project_root / ".meridian" / "spawns" / "p-parent"
    task_cwd.mkdir(parents=True)
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        execution_cwd=task_cwd,
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        _ = command, output_log_path
        captured["cwd"] = cwd
        captured["task_env"] = dict(env).get("MERIDIAN_TASK_CWD")
        assert callable(on_child_started)
        on_child_started(4445)
        return (0, 4445)

    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert captured["cwd"] == project_root
    assert captured["task_env"] == task_cwd.as_posix()
    assert_task_cwd_instruction(
        launch_context.binding.run_params.appended_system_prompt or "",
        task_cwd,
    )
    projected_roots = {path.resolve() for path in launch_context.binding.spec.projected_roots}
    assert task_cwd.resolve() not in projected_roots
    assert outcome.exit_code == 0


@pytest.mark.slow
def test_run_harness_process_claude_primary_print_json_persists_session_id_from_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "claude-print-json"
    project_root.mkdir()
    emitted_session_id = "c75c9a5c-1234-5678-9abc-def012345678"
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        extra_args=("--print", "--output-format", "json"),
    )
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert isinstance(output_log_path, Path)
        captured["output_log_path"] = output_log_path
        output_log_path.write_text(
            json.dumps({"session_id": emitted_session_id, "result": "ok"}) + "\n",
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(445)
        return (0, 445)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert isinstance(captured["output_log_path"], Path)
    assert captured["output_log_path"].name == OUTPUT_FILENAME
    assert outcome.resolved_harness_session_id == emitted_session_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == emitted_session_id


@pytest.mark.slow
def test_run_harness_process_claude_primary_stays_on_black_box_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "claude-compat"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    black_box_calls = 0

    def fail_managed(
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
        _ = (
            harness_id,
            spawn_id,
            spawn_dir,
            control_root,
            task_cwd,
            env,
            spec,
            process_launcher,
            on_running,
        )
        raise AssertionError("claude primary must not use managed launcher path")

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        nonlocal black_box_calls
        black_box_calls += 1
        assert callable(on_child_started)
        on_child_started(7272)
        return (0, 7272)

    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fail_managed,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert black_box_calls == 1
    assert outcome.exit_code == 0


def test_explicit_no_agent_skips_claude_native_agent_projection(
    tmp_path: Path,
) -> None:
    project_root = tmp_path
    (project_root / "meridian.toml").write_text(
        '[primary]\nagent = "product-lead"\n',
        encoding="utf-8",
    )
    from tests.support.fixtures import write_agent

    write_agent(
        project_root,
        name="product-lead",
        model="claude-sonnet-4-5",
        body="# Product Lead",
    )
    launch_context, _ = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        agent_opt_out=True,
    )

    command = " ".join(launch_context.binding.argv)
    assert "--agent" not in command
    assert "--agents" not in command
    assert launch_context.binding.spec.agent_name is None
    assert launch_context.binding.spec.agents_payload is None
