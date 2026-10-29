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

from meridian.lib.config.loader import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.launch.process.runner import PrimaryProcessResult, run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.session_links import session_task_link_path
from meridian.lib.platform.launch_gate import ProcessIdentity
from meridian.lib.state.spawn_store import list_spawns
from tests.support.fixtures import write_agent
from tests.support.launch import (
    assert_task_cwd_instruction,
    build_launch_context,
    stub_bundle_request_and_resolve,
)


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
    harness_registry.get_subprocess_harness(HarnessId.CLAUDE)

    def fake_run_primary_process_with_capture(
        command: tuple[str, ...],
        cwd: Any,
        env: Any,
        output_log_path: Any,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = cwd, env, launcher_diagnostics_path
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
        on_child_started(ProcessIdentity(222, 100.0, 222))
        return PrimaryProcessResult(0, 222, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
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
    manifest = json.loads((log_dir / "projection-manifest.json").read_text(encoding="utf-8"))
    assert manifest.pop("permission_report")
    assert manifest == {
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
def test_claude_primary_prompt_file_holds_each_loaded_skill_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The file Claude reads as its system prompt carries every loaded skill, once."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    skills = (
        LoadedSkillEntry(name="dev-principles", skill_type="principle", body="Prefer deletion."),
        LoadedSkillEntry(name="verification", skill_type="reference", body="Check each edge."),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
        skills_loaded=skills,
    )
    write_agent(
        tmp_path,
        name="solo-dev",
        model="claude-sonnet-4-5",
        skills=tuple(skill.name for skill in skills),
    )
    _write_minimal_mars_config(tmp_path)
    harness_registry = get_default_harness_registry()
    config = load_config(tmp_path)
    launch_context = build_launch_context(
        spawn_id="dry-run-primary-skills",
        request=SpawnRequest(
            prompt="primary prompt",
            model="claude-sonnet-4-5",
            harness=HarnessId.CLAUDE.value,
            agent="solo-dev",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    captured: dict[str, str] = {}

    def fake_run_primary_process_with_capture(
        command: tuple[str, ...],
        cwd: Any,
        env: Any,
        output_log_path: Any,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> PrimaryProcessResult:
        _ = cwd, env, output_log_path, process_launcher, launcher_diagnostics_path
        command = tuple(command)
        prompt_file = Path(command[command.index("--append-system-prompt-file") + 1])
        captured["prompt_file_text"] = prompt_file.read_text(encoding="utf-8")
        on_child_started(ProcessIdentity(222, 100.0, 222))
        return PrimaryProcessResult(0, 222, None)

    run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    prompt_file_text = captured["prompt_file_text"]
    for skill in skills:
        assert prompt_file_text.count(f"# Skill: /{skill.name}") == 1
        assert prompt_file_text.count(skill.body) == 1


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
    harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = launcher_diagnostics_path
        _ = command, output_log_path
        captured["cwd"] = cwd
        captured["task_env"] = dict(env).get("MERIDIAN_TASK_DIR")
        captured["chat_env"] = dict(env).get("MERIDIAN_CHAT_ID")
        captured["project_env"] = dict(env).get("MERIDIAN_PROJECT_DIR")
        assert callable(on_child_started)
        on_child_started(ProcessIdentity(4445, 100.0, 4445))
        return PrimaryProcessResult(0, 4445, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    assert captured["cwd"] == project_root
    task_link = session_task_link_path(
        Path(launch_context.runtime_root),
        str(captured["chat_env"]),
    )
    assert captured["task_env"] == task_link.as_posix()
    assert captured["project_env"] == project_root.as_posix()
    # The preview has no chat, so its prompt names the launch's own checkout; the
    # run's environment names the per-chat link the session keeps using.
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
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = command, cwd, env, launcher_diagnostics_path
        assert isinstance(output_log_path, Path)
        captured["output_log_path"] = output_log_path
        output_log_path.write_text(
            json.dumps({"session_id": emitted_session_id, "result": "ok"}) + "\n",
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(ProcessIdentity(445, 100.0, 445))
        return PrimaryProcessResult(0, 445, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    assert isinstance(captured["output_log_path"], Path)
    assert captured["output_log_path"].name == "output.jsonl"
    assert outcome.resolved_harness_session_id != emitted_session_id
    assert outcome.resolved_harness_session_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == outcome.resolved_harness_session_id


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
    harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    black_box_calls = 0

    def fail_managed(
        harness_id: Any,
        launch: Any,
        control_root: Any,
        task_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
        session_id_observer: Any = None,
    ) -> PrimaryAttachOutcome:
        _ = (
            harness_id,
            launch,
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
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = command, cwd, env, output_log_path, launcher_diagnostics_path
        nonlocal black_box_calls
        black_box_calls += 1
        assert callable(on_child_started)
        on_child_started(ProcessIdentity(7272, 100.0, 7272))
        return PrimaryProcessResult(0, 7272, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_attach_fn=fail_managed,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
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


@pytest.mark.slow
def test_primary_print_survives_corrupt_stdout_and_fold_drift(monkeypatch, tmp_path):
    from meridian.lib.harness.extractors.claude import ClaudeFold

    project = tmp_path / "corrupt-print"
    project.mkdir()
    context, registry = _build_primary_launch_context(
        project_root=project,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        extra_args=("--print",),
    )
    original_fold = ClaudeFold.fold_event

    def drifting_fold(self, kind, payload):
        if kind == "broken":
            raise ValueError("format drift")
        original_fold(self, kind, payload)

    def captured_child(
        command,
        cwd,
        env,
        output_log_path,
        process_launcher,
        on_child_started,
        launcher_diagnostics_path,
    ):
        _ = command, cwd, env, launcher_diagnostics_path
        output_log_path.write_bytes(
            b'\xff\n{"type":"broken"}\n'
            b'{"type":"result","result":"surviving report","usage":{"input_tokens":7}}\n'
        )
        on_child_started(ProcessIdentity(445, 100.0, 445))
        return PrimaryProcessResult(0, 445, None)

    monkeypatch.setattr(ClaudeFold, "fold_event", drifting_fold)
    outcome = run_harness_process(
        context,
        registry,
        prepared=context.prepared,
        run_primary_process_with_capture_fn=captured_child,
    )
    assert outcome.exit_code == 0
    row = list_spawns(context.runtime_root).records[0]
    assert row.status == "succeeded"
    assert row.terminal.input_tokens == 7
    assert (
        "surviving report" in (context.runtime_root / "spawns" / row.id / "report.md").read_text()
    )
