# qa-validated: test-suite-redesign
"""Spawn prepare surface tests: reference routing, channel manifests, and inventory placement."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from tests.support.fixtures import write_agent
from tests.support.launch import stub_bundle_request_and_resolve

pytestmark = pytest.mark.slow

_BUNDLE_INVENTORY = (
    "# Meridian Agents\n\n"
    "## Subagent\n"
    "- `meridian spawn -a dev-orchestrator`: Orchestrate.\n"
    "- `meridian spawn -a reviewer`: Review.\n"
)


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def test_spawn_prepare_opencode_keeps_all_references_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
        prompt_surface_inventory_prompt=(
            "# Meridian Agents\n\n"
            "## Subagent\n"
            "- `meridian spawn -a dev-orchestrator`: Orchestrate.\n"
        ),
    )
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


def test_spawn_prepare_profile_resolved_claude_approval_auto_projects_accept_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(approval="auto"),
        provenance={
            "model_source": "profile-default",
            "harness_source": "provider",
            "approval_source": "profile-default",
        },
    )
    write_agent(tmp_path, name="coder", model="claude-sonnet-4-6")

    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare-approval-auto",
        request=SpawnRequest(
            prompt="task prompt",
            prompt_is_composed=False,
            agent="coder",
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

    assert preview.resolved_request.execution_policy.approval == "auto"
    assert "--permission-mode" in preview.binding.argv
    assert "acceptEdits" in preview.binding.argv


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        ("codex", "gpt-5.4"),
    ],
)
def test_spawn_prepare_system_field_harnesses_route_agent_inventory_to_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    model: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    expected_harness = HarnessId(harness)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=model,
        harness=expected_harness,
        prompt_surface_inventory_prompt=_BUNDLE_INVENTORY,
    )

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
    assert "`meridian spawn -a dev-orchestrator`" in inventory_channel
    assert "`meridian spawn -a reviewer`" in inventory_channel
    assert "# Meridian Agents" not in preview.projected_content.user_turn_content
    assert "# Meridian Agents" not in preview.resolved_request.prompt


def test_spawn_prepare_claude_projects_skills_inventory_and_report_to_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
        skills_loaded=(
            LoadedSkillEntry(
                name="verification",
                skill_type="reference",
                body="Use verification checklist.",
            ),
        ),
        prompt_surface_inventory_prompt=_BUNDLE_INVENTORY,
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )
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
    assert "`meridian spawn -a dev-orchestrator`" in projected.system_prompt
    assert "`meridian spawn -a reviewer`" in projected.system_prompt
    assert "# Report" in projected.system_prompt
    assert "final assistant message must be the run report" in projected.system_prompt

    # Skills still delivered via --append-system-prompt for Claude.
    # The argv uses --append-system-prompt-file, so skill content is in the
    # system prompt file content, not directly in argv.
    assert any("--append-system-prompt-file" in str(arg) for arg in preview.binding.argv)
    # Verify skill content is actually in the appended payload, not just the flag.
    assert preview.binding.run_params.appended_system_prompt is not None
    assert "Use verification checklist." in preview.binding.run_params.appended_system_prompt

    assert "complete the task" in projected.user_turn_content
    assert f"# Reference: {file_ref.as_posix()}" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content

    assert preview.binding.run_params.prompt == projected.user_turn_content
    assert "# Skill:" not in preview.binding.run_params.prompt
    assert "# Meridian Agents" not in preview.binding.run_params.prompt


def test_spawn_prepare_claude_continue_session_keeps_skills_in_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
        skills_loaded=(
            LoadedSkillEntry(
                name="verification",
                skill_type="reference",
                body="Use verification checklist.",
            ),
        ),
        prompt_surface_inventory_prompt=_BUNDLE_INVENTORY,
    )
    write_agent(
        tmp_path,
        name="dev-orchestrator",
        model="claude-sonnet-4-5",
        skills=("verification",),
    )

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
    assert preview.binding.run_params.appended_system_prompt is not None
    assert "Use verification checklist." in preview.binding.run_params.appended_system_prompt

    assert "continue the task" in projected.user_turn_content
    assert "# Skill:" not in projected.user_turn_content
    assert "# Meridian Agents" not in projected.user_turn_content


def test_spawn_prepare_claude_projects_profile_auto_approval_without_cli_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(approval="auto"),
    )

    preview = build_launch_context(
        spawn_id="dry-run-claude-spawn-prepare-approval-auto",
        request=SpawnRequest(
            prompt="task",
            prompt_is_composed=False,
            model="claude-sonnet-4-5",
            harness="claude",
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

    argv = preview.binding.argv
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv


def test_spawn_prepare_headless_deny_error_names_denied_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    (tmp_path / "meridian.toml").write_text(
        "[spawn]\n"
        'deny_headless_harnesses = ["codex"]\n',
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )

    with pytest.raises(ValueError) as exc_info:
        build_launch_context(
            spawn_id="dry-run-codex-spawn-denied",
            request=SpawnRequest(
                prompt="task",
                prompt_is_composed=False,
                model="gpt-5.4",
                harness="codex",
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

    message = str(exc_info.value)
    assert "Headless spawns on the 'codex' harness are denied" in message
    # Cross-harness path: caller is not on codex, so suggest -m / --harness.
    assert "-m <model>" in message
    assert "route to an allowed harness" in message


def test_spawn_prepare_headless_deny_same_harness_suggests_native_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-harness caller gets pointed to native Agent() tool."""
    _write_minimal_mars_config(tmp_path)
    (tmp_path / "meridian.toml").write_text(
        "[spawn]\n"
        'deny_headless_harnesses = ["claude"]\n',
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-opus-4-6",
        harness=HarnessId.CLAUDE,
    )
    monkeypatch.setenv("MERIDIAN_HARNESS", "claude")

    with pytest.raises(ValueError) as exc_info:
        build_launch_context(
            spawn_id="dry-run-claude-spawn-denied",
            request=SpawnRequest(
                prompt="task",
                prompt_is_composed=False,
                model="claude-opus-4-6",
                harness="claude",
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

    message = str(exc_info.value)
    assert "Headless spawns on the 'claude' harness are denied" in message
    # Same-harness path: suggest native Agent() tool (no agent name since none specified).
    assert "Agent() tool" in message
