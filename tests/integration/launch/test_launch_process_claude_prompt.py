# qa-validated: test-suite-redesign
"""Claude primary launch identity, task directory, and interruption tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.loader import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
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
def test_run_harness_process_claude_primary_print_json_keeps_assigned_session_id(
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


def test_keyboard_interrupt_in_black_box_wait_finalizes_cancelled_130(tmp_path: Path) -> None:
    context, registry = _build_primary_launch_context(
        project_root=tmp_path, harness_id=HarnessId.CLAUDE, model="claude-sonnet-4-5"
    )

    def interrupted_child(
        command,
        cwd,
        env,
        output_log_path,
        process_launcher,
        on_child_started,
        launcher_diagnostics_path,
    ):
        _ = command, cwd, env, output_log_path, launcher_diagnostics_path
        on_child_started(ProcessIdentity(446, 100.0, 446))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_harness_process(
            context,
            registry,
            prepared=context.prepared,
            run_primary_process_with_capture_fn=interrupted_child,
        )
    row = list_spawns(context.runtime_root).records[0]
    assert row.status == "cancelled"
    assert row.terminal.exit_code == 130
