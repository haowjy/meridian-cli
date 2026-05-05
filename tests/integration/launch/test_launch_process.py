from __future__ import annotations

import json
import os

# pyright: reportPrivateUsage=false
import sys
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.launch_spec import CodexLaunchSpec
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import command as launch_command
from meridian.lib.launch import process
from meridian.lib.launch.constants import (
    OUTPUT_FILENAME,
    PRIMARY_META_FILENAME,
)
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


def test_subprocess_launcher_captures_output_log(tmp_path: Path) -> None:
    output_log_path = tmp_path / "history.jsonl"
    launched = SubprocessProcessLauncher().launch(
        command=(
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.write('line-1\\n');"
                "sys.stdout.flush();"
                "sys.stderr.write('line-2\\n');"
                "sys.stderr.flush()"
            ),
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        output_log_path=output_log_path,
    )

    assert launched.exit_code == 0
    assert output_log_path.read_text(encoding="utf-8").splitlines() == ["line-1", "line-2"]


@pytest.mark.slow
def test_run_harness_process_fork_uses_new_chat_and_materialized_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    launch_context = build_launch_context(
        spawn_id="dry-run-primary",
        request=SpawnRequest(
            prompt="fork prompt",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_chat_id="c7",
                forked_from_chat_id="c7",
                continue_fork=True,
                primary_session_mode=SessionMode.FORK.value,
            ),
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

    captured: dict[str, str | None] = {}

    def fake_project_codex_spec_to_cli_args(
        spec: CodexLaunchSpec,
        *,
        base_command: tuple[str, ...],
    ) -> list[str]:
        captured["build_continue_session"] = spec.continue_session_id
        return [*base_command, "resume", spec.continue_session_id or ""]

    def fake_fork_session(source_session_id: str) -> str:
        captured["fork_source_session"] = source_session_id
        return "forked-session"

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        captured["env_chat_id"] = dict(kwargs["env"]).get("MERIDIAN_CHAT_ID")
        return process.PrimaryAttachOutcome(
            exit_code=0, session_id="forked-session", tui_pid=111,
        )

    def fake_start_session(
        runtime_root: Path,
        harness: str,
        harness_session_id: str | None,
        model: str,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        _ = (runtime_root, harness, model)
        captured["chat_id_arg"] = chat_id
        captured["start_harness_session_id"] = harness_session_id
        captured["forked_from_chat_id"] = kwargs.get("forked_from_chat_id")
        return "c999"

    monkeypatch.setattr(
        launch_command,
        "project_codex_spec_to_cli_args",
        fake_project_codex_spec_to_cli_args,
    )
    monkeypatch.setattr(codex_adapter, "fork_session", fake_fork_session)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: "forked-session")
    monkeypatch.setattr(
        process,
        "_run_primary_attach",
        fake_run_primary_attach,
    )
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "start_session", fake_start_session)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert captured["fork_source_session"] == "source-session"
    assert captured["build_continue_session"] == "forked-session"
    assert captured["chat_id_arg"] is None
    # I-10: session is created with the SOURCE session ID; fork happens after the row exists.
    assert captured["start_harness_session_id"] == "source-session"
    assert captured["forked_from_chat_id"] == "c7"
    assert captured["env_chat_id"] == "c999"
    assert outcome.chat_id == "c999"
    events = [
        json.loads(line)
        for line in (launch_context.runtime_root / "spawns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    finalize_events = [event for event in events if event.get("event") == "finalize"]
    assert len(finalize_events) == 1
    assert finalize_events[0]["origin"] == "launcher"


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

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        command = tuple(kwargs["command"])
        captured["command"] = command
        output_log_path = kwargs["output_log_path"]
        captured["output_log_path"] = output_log_path
        prompt_flag_index = command.index("--append-system-prompt-file")
        prompt_file_path = Path(command[prompt_flag_index + 1])
        captured["prompt_file_exists"] = prompt_file_path.exists()
        captured["prompt_file_text"] = (
            prompt_file_path.read_text(encoding="utf-8")
            if prompt_file_path.exists()
            else None
        )
        captured["log_dir"] = prompt_file_path.parent
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(222)
        return (0, 222)

    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

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
def test_run_harness_process_writes_codex_system_field_primary_projection_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    def fake_launcher_for(captured: dict[str, object]):
        def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
            spawn_dir = Path(kwargs["spawn_dir"])
            captured["log_dir"] = spawn_dir
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
    monkeypatch.setattr(
        process,
        "_run_primary_attach",
        fake_launcher_for(captured),
    )
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("managed primary path should avoid black-box launcher")
        ),
    )
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

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
def test_run_harness_process_writes_opencode_system_field_primary_projection_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)

    def fake_launcher_for(captured: dict[str, object]):
        def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
            spawn_dir = (
                kwargs.get("spawn_dir")
                or kwargs.get("spawn_log_dir")
                or kwargs.get("log_dir")
            )
            assert isinstance(spawn_dir, Path)
            captured["log_dir"] = spawn_dir
            return process.PrimaryAttachOutcome(exit_code=0, session_id=None, tui_pid=333)

        return fake_run_primary_attach

    harness_id = HarnessId.OPENCODE
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
            model="gemini-2.5-pro",
            harness=harness_id.value,
            extra_args=(
                f"--append-system-prompt={harness_id.value} passthrough system prompt",
            ),
            session=SessionRequest(
                requested_harness_session_id="existing-opencode-session",
                continue_chat_id="c-opencode",
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
    monkeypatch.setattr(process, "_run_primary_attach", fake_launcher_for(captured))
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("managed primary path should avoid black-box launcher")
        ),
    )
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    log_dir = captured["log_dir"]
    assert isinstance(log_dir, Path)
    system_prompt = (log_dir / "system-prompt.md").read_text(encoding="utf-8")
    assert f"{harness_id.value} passthrough system prompt" in system_prompt
    assert f"{harness_id.value} primary prompt" not in system_prompt
    starting_prompt = (log_dir / "starting-prompt.md").read_text(encoding="utf-8")
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
def test_run_harness_process_black_box_primary_uses_no_tui_log_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "repo"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        captured["output_log_path"] = kwargs["output_log_path"]
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(444)
        return (0, 444)

    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert captured["output_log_path"] is None
    assert outcome.primary_spawn_id is not None
    assert list(launch_context.runtime_root.rglob("tui.log")) == []


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

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        captured["harness_id"] = kwargs["harness_id"]
        spawn_dir = Path(kwargs["spawn_dir"])
        spawn_dir.mkdir(parents=True, exist_ok=True)
        captured["spawn_dir"] = spawn_dir
        return process.PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5150)

    def fail_black_box(**kwargs: object) -> tuple[int, int]:
        _ = kwargs
        raise AssertionError("codex primary should use managed launcher path")

    monkeypatch.setattr(process, "_run_primary_attach", fake_run_primary_attach)
    monkeypatch.setattr(process, "_run_primary_process_with_capture", fail_black_box)
    monkeypatch.setattr(process_runner, "select_process_launcher", fake_select_process_launcher)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

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

    def _read_spawn_events() -> list[dict[str, object]]:
        spawns_jsonl = launch_context.runtime_root / "spawns.jsonl"
        if not spawns_jsonl.exists():
            return []
        return [
            json.loads(line)
            for line in spawns_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _running_updates(events: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            event
            for event in events
            if event.get("event") == "update" and event.get("status") == "running"
        ]

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        on_running = kwargs.get("on_running")
        assert callable(on_running)
        assert _running_updates(_read_spawn_events()) == []
        on_running(5151)
        updates_after_callback = _running_updates(_read_spawn_events())
        captured["updates_seen_before_return"] = len(updates_after_callback)
        return process.PrimaryAttachOutcome(exit_code=0, session_id="thread-managed", tui_pid=5151)

    def fail_black_box(**kwargs: object) -> tuple[int, int]:
        _ = kwargs
        raise AssertionError("codex primary should use managed launcher path")

    monkeypatch.setattr(process, "_run_primary_attach", fake_run_primary_attach)
    monkeypatch.setattr(process, "_run_primary_process_with_capture", fail_black_box)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert captured["updates_seen_before_return"] == 1
    running_updates = _running_updates(_read_spawn_events())
    assert len(running_updates) == 1
    assert running_updates[0]["worker_pid"] == 5151
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "thread-managed"


@pytest.mark.slow
def test_run_harness_process_opencode_primary_routes_to_managed_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "opencode-managed"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.OPENCODE,
        model="gemini-2.5-pro",
        session=SessionRequest(
            requested_harness_session_id="existing-opencode-session",
            continue_chat_id="c-opencode",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    opencode_adapter = harness_registry.get_subprocess_harness(HarnessId.OPENCODE)
    captured: dict[str, object] = {}

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        captured["harness_id"] = kwargs["harness_id"]
        return process.PrimaryAttachOutcome(exit_code=0, session_id="session-managed", tui_pid=6262)

    def fail_black_box(**kwargs: object) -> tuple[int, int]:
        _ = kwargs
        raise AssertionError("opencode primary should use managed launcher path")

    monkeypatch.setattr(process, "_run_primary_attach", fake_run_primary_attach)
    monkeypatch.setattr(process, "_run_primary_process_with_capture", fail_black_box)
    monkeypatch.setattr(opencode_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert captured["harness_id"] == HarnessId.OPENCODE
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "session-managed"


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

    def fail_managed(**kwargs: object) -> process.PrimaryAttachOutcome:
        _ = kwargs
        raise AssertionError("claude primary must not use managed launcher path")

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        nonlocal black_box_calls
        black_box_calls += 1
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(7272)
        return (0, 7272)

    monkeypatch.setattr(process, "_run_primary_attach", fail_managed)
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert black_box_calls == 1
    assert outcome.exit_code == 0


@pytest.mark.slow
def test_run_harness_process_opencode_fork_uses_managed_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenCode fork routes through managed backend (same as all other modes)."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "opencode-fork"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.OPENCODE,
        model="gemini-2.5-pro",
        session=SessionRequest(
            requested_harness_session_id="source-session",
            continue_chat_id="c17",
            continue_fork=True,
            primary_session_mode=SessionMode.FORK.value,
        ),
    )
    opencode_adapter = harness_registry.get_subprocess_harness(HarnessId.OPENCODE)
    managed_calls = 0

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        nonlocal managed_calls
        managed_calls += 1
        on_running = kwargs.get("on_running")
        if callable(on_running):
            on_running(8383)
        return process.PrimaryAttachOutcome(exit_code=0, session_id="oc-session", tui_pid=8383)

    monkeypatch.setattr(process, "_run_primary_attach", fake_run_primary_attach)
    monkeypatch.setattr(opencode_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert managed_calls == 1
    assert outcome.exit_code == 0


@pytest.mark.slow
def test_run_harness_process_codex_managed_failure_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex primary must use managed backend; failure raises error, no fallback."""
    import pytest as _pytest
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

    def failing_managed(**kwargs: object) -> process.PrimaryAttachOutcome:
        spawn_dir = Path(kwargs["spawn_dir"])
        spawn_dir.mkdir(parents=True, exist_ok=True)
        raise process.PrimaryAttachError("managed startup error")

    monkeypatch.setattr(process, "_run_primary_attach", failing_managed)
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("codex should not fall back to black-box")
        ),
    )
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    with _pytest.raises(process.PrimaryAttachError, match="managed startup error"):
        process.run_harness_process(launch_context, harness_registry)


@pytest.mark.slow
def test_run_harness_process_managed_failure_falls_back_to_black_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenCode can fall back to black-box when managed backend fails."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "opencode-fallback"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.OPENCODE,
        model="gemini-2.5-pro",
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

    def failing_managed(**kwargs: object) -> process.PrimaryAttachOutcome:
        nonlocal managed_calls
        nonlocal captured_spawn_dir
        managed_calls += 1
        spawn_dir = Path(kwargs["spawn_dir"])
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
        raise process.PrimaryAttachError("managed startup error")

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        nonlocal black_box_calls
        black_box_calls += 1
        assert kwargs["output_log_path"] is None
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(9494)
        return (0, 9494)

    monkeypatch.setattr(process, "_run_primary_attach", failing_managed)
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(opencode_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert managed_calls == 1
    assert black_box_calls == 1
    assert captured_spawn_dir is not None
    assert not (captured_spawn_dir / PRIMARY_META_FILENAME).exists()
    assert not (captured_spawn_dir / OUTPUT_FILENAME).exists()
    assert list(launch_context.runtime_root.rglob("tui.log")) == []
    assert outcome.exit_code == 0




@pytest.mark.slow
def test_run_harness_process_restores_claude_config_dir_when_isolation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "claude-isolation-fallback"
    project_root.mkdir()
    custom_config = tmp_path / "custom-claude"
    custom_config.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_prepare_isolated_claude_config(**kwargs: object) -> tuple[Path | None, str]:
        return None, custom_config.as_posix()

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        captured["claude_config_dir"] = env.get("CLAUDE_CONFIG_DIR")
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(777)
        return (0, 777)

    monkeypatch.setattr(
        "meridian.lib.harness.claude_preflight.prepare_isolated_claude_config",
        fake_prepare_isolated_claude_config,
    )
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert outcome.exit_code == 0
    assert captured["claude_config_dir"] == custom_config.as_posix()
    events = [
        json.loads(line)
        for line in (launch_context.runtime_root / "spawns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(
        event.get("event") == "update"
        and event.get("claude_config_dir") == custom_config.as_posix()
        for event in events
    )
    sessions = [
        json.loads(line)
        for line in (launch_context.runtime_root / "sessions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(
        event.get("event") == "update"
        and event.get("claude_config_dir") == custom_config.as_posix()
        for event in sessions
    )

@pytest.mark.slow
def test_run_harness_process_fresh_claude_primary_does_not_inject_seed_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh Claude primary launch must not inject unsupported --session-id args."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-reuse"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        command = tuple(kwargs["command"])
        captured["command"] = command
        if "--session-id" in command:
            idx = command.index("--session-id")
            captured["command_session_id"] = command[idx + 1]
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(555)
        return (0, 555)

    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert launch_context.seed_harness_session_id in (None, "")
    assert "command_session_id" not in captured
    assert outcome.resolved_harness_session_id == ""
    events = [
        json.loads(line)
        for line in (launch_context.runtime_root / "spawns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    start_events = [e for e in events if e.get("event") == "start"]
    assert len(start_events) == 1
    assert start_events[0]["harness_session_id"] in (None, "")


@pytest.mark.slow
def test_run_harness_process_repairs_state_when_observed_session_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Observation repairs state when harness uses a different session than persisted."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-repair"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    observed_id = "observed-different-session"

    def fake_run_primary_process_with_capture(**kwargs: object) -> tuple[int, int]:
        started = kwargs.get("on_child_started")
        assert callable(started)
        started(666)
        return (0, 666)

    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        fake_run_primary_process_with_capture,
    )
    monkeypatch.setattr(
        claude_adapter, "observe_session_id", lambda **kwargs: observed_id
    )
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    # State should be repaired to the observed session ID
    assert outcome.resolved_harness_session_id == observed_id
    # Spawn record should have the observed ID
    events = [
        json.loads(line)
        for line in (launch_context.runtime_root / "spawns.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    update_events = [
        e
        for e in events
        if e.get("event") == "update" and e.get("harness_session_id") == observed_id
    ]
    assert len(update_events) >= 1


@pytest.mark.slow
def test_run_harness_process_resume_does_not_inject_seed_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resume launches must not inject seed session args into passthrough."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-resume"
    project_root.mkdir()
    launch_context, _harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="existing-session-id",
            continue_chat_id="c42",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    # Resume path: adapter returns the existing session ID, no session_args injection
    assert launch_context.seed_harness_session_args == ()
    assert launch_context.seed_harness_session_id == "existing-session-id"


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

    def fake_run_primary_attach(**kwargs: object) -> process.PrimaryAttachOutcome:
        nonlocal managed_calls
        managed_calls += 1
        # Verify this is for Codex
        assert kwargs["harness_id"] == HarnessId.CODEX
        # Call on_running to mark spawn as running
        on_running = kwargs.get("on_running")
        if callable(on_running):
            on_running(12345)
        return process.PrimaryAttachOutcome(
            exit_code=0, session_id="fresh-thread-id", tui_pid=12345
        )

    monkeypatch.setattr(process, "_run_primary_attach", fake_run_primary_attach)
    monkeypatch.setattr(
        process,
        "_run_primary_process_with_capture",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("fresh codex primary should use managed path, not black-box")
        ),
    )
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: None)
    monkeypatch.setattr(process, "stop_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(process, "update_session_harness_id", lambda *args, **kwargs: None)

    outcome = process.run_harness_process(launch_context, harness_registry)

    assert managed_calls == 1
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "fresh-thread-id"
