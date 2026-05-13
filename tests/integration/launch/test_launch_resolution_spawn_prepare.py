# qa-validated: test-suite-redesign
"""Spawn prepare surface tests: reference routing, channel manifests, and inventory placement."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from tests.support.fixtures import write_agent, write_skill

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def test_spawn_prepare_opencode_keeps_all_references_inline(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model="claude-sonnet-4-5")
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")
    file_ref = tmp_path / "README.md"
    file_ref.write_text("# hello\n", encoding="utf-8")
    dir_ref = tmp_path / "src"
    dir_ref.mkdir()
    (dir_ref / "main.py").write_text("print('ok')\n", encoding="utf-8")

    preview = build_launch_context(
        spawn_id="dry-run-opencode-spawn-prepare",
        request=SpawnRequest(
            prompt="task prompt",
            prompt_is_composed=False,
            model="gemini-2.5-pro",
            harness="opencode",
            reference_files=(file_ref.as_posix(), dir_ref.as_posix()),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert "--file" not in preview.binding.argv
    assert file_ref.as_posix() not in preview.binding.argv
    assert preview.projected_content is not None
    assert [route.to_dict() for route in preview.projected_content.reference_routing] == [
        {
            "path": file_ref.as_posix(),
            "type": "file",
            "routing": "inline",
            "native_flag": None,
        },
        {
            "path": dir_ref.as_posix(),
            "type": "directory",
            "routing": "inline",
            "native_flag": None,
        },
    ]
    assert preview.projected_content.channel_manifest() == {
        "system_instruction": "system-field",
        "user_task_prompt": "user-turn",
        "task_context": "user-turn",
    }
    assert f"# Reference: {file_ref.as_posix()}" in preview.resolved_request.prompt
    assert f"# Reference: {dir_ref.as_posix()}/" in preview.resolved_request.prompt
    assert "# Meridian Agents" not in preview.resolved_request.prompt
    assert "# Meridian Agents" in preview.projected_content.system_prompt


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        ("codex", "gpt-5.4"),
        ("opencode", "gemini-2.5-pro"),
    ],
)
def test_spawn_prepare_system_field_harnesses_route_agent_inventory_to_system_prompt(
    tmp_path: Path,
    harness: str,
    model: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(tmp_path, name="dev-orchestrator", model="claude-sonnet-4-5")
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")

    preview = build_launch_context(
        spawn_id=f"dry-run-{harness}-spawn-prepare-no-inventory",
        request=SpawnRequest(
            prompt="task prompt",
            prompt_is_composed=False,
            model=model,
            harness=harness,
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    inventory_channel = preview.projected_content.system_prompt
    assert "# Meridian Agents" in inventory_channel
    assert "## Subagent" in inventory_channel
    assert "- dev-orchestrator" in inventory_channel
    assert "- reviewer" in inventory_channel
    assert "# Meridian Agents" not in preview.projected_content.user_turn_content
    assert "# Meridian Agents" not in preview.resolved_request.prompt


def test_spawn_prepare_claude_projects_skills_inventory_and_report_to_system_prompt(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_skill(
        tmp_path,
        "verification",
        body="Use verification checklist.",
        description="Verification helper",
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")
    file_ref = tmp_path / "README.md"
    file_ref.write_text("# project\n", encoding="utf-8")

    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare",
        request=SpawnRequest(
            prompt="complete the task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
            reference_files=(file_ref.as_posix(),),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    projected = preview.projected_content

    assert preview.resolved_request.skill_paths


    # Claude declares supports_native_skills=True, so skill content is
    # suppressed from supplemental_documents (projected.system_prompt).
    # Skills are delivered via compose_skill_injections() → appended_system_prompt.
    assert "# Skill:" not in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "## Subagent" in projected.system_prompt
    assert "- dev-orchestrator" in projected.system_prompt
    assert "- reviewer" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    assert "final assistant message must be the run report" in projected.system_prompt

    # Skills still delivered via --append-system-prompt for Claude.
    # The argv uses --append-system-prompt-file, so skill content is in the
    # system prompt file content, not directly in argv.
    assert any("--append-system-prompt-file" in str(arg) for arg in preview.binding.argv)

    assert "complete the task" in projected.user_turn_content
    assert f"# Reference: {file_ref.as_posix()}" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content

    assert preview.binding.run_params.prompt == projected.user_turn_content
    assert "# Skill:" not in preview.binding.run_params.prompt
    assert "# Meridian Agents" not in preview.binding.run_params.prompt


def test_spawn_prepare_claude_continue_session_keeps_skills_in_system_prompt(
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_skill(
        tmp_path,
        "verification",
        body="Use verification checklist.",
        description="Verification helper",
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )
    write_agent(tmp_path, name="reviewer", model="gpt-5.4")

    harness_session_id = "claude-session-123"
    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare-continue",
        request=SpawnRequest(
            prompt="continue the task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
            agent="dev-orchestrator",
            session=SessionRequest(
                requested_harness_session_id=harness_session_id,
                continue_harness="claude",
                continue_fork=True,
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert preview.projected_content is not None
    projected = preview.projected_content

    assert preview.binding.run_params.continue_harness_session_id == harness_session_id
    assert preview.binding.run_params.continue_fork is True
    # Claude declares supports_native_skills=True, so skill content is
    # suppressed from supplemental_documents (projected.system_prompt).
    # Skills are delivered via compose_skill_injections() → appended_system_prompt.
    assert "# Skill:" not in projected.system_prompt
    assert "# Meridian Agents" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    # Skills still delivered via --append-system-prompt-file
    assert any("--append-system-prompt-file" in str(arg) for arg in preview.binding.argv)

    assert "continue the task" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content
