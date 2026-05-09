"""Unit tests for semantic launch content projection."""

from pathlib import Path

from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec, CodexLaunchSpec
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.launch.composition import ComposedLaunchContent, PromptDocument
from meridian.lib.launch.reference import ReferenceItem
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _content() -> ComposedLaunchContent:
    return ComposedLaunchContent(
        supplemental_documents=(
            PromptDocument(
                kind="skill",
                logical_name="skill",
                path="/repo/skill/SKILL.md",
                content="SYSTEM: skill content",
            ),
        ),
        agent_profile_body="SYSTEM: profile body",
        report_instruction="SYSTEM: report instruction",
        inventory_prompt="SYSTEM: agent inventory",
        context_prompt="",
        completion_contract="SYSTEM: completion contract",
        passthrough_system_fragments=("SYSTEM: passthrough fragment",),
        user_task_prompt="USER: task prompt",
        reference_items=(
            ReferenceItem(
                kind="file",
                path=Path("/repo/ref.txt"),
                body="CONTEXT: reference file",
            ),
        ),
        prior_output="CONTEXT: prior output",
    )


def _resolver() -> TieredPermissionResolver:
    return TieredPermissionResolver(config=PermissionConfig())


def _assert_ordered(text: str, expected_parts: tuple[str, ...]) -> None:
    positions = [text.index(part) for part in expected_parts]
    assert positions == sorted(positions)


def test_claude_project_content_routes_system_separately_from_user_turn() -> None:
    projected = ClaudeAdapter().project_content(_content())
    system_sentinels = (
        "SYSTEM: skill content",
        "SYSTEM: profile body",
        "SYSTEM: report instruction",
        "SYSTEM: agent inventory",
        "SYSTEM: completion contract",
        "SYSTEM: passthrough fragment",
    )
    user_turn_sentinels = (
        "USER: task prompt",
        "CONTEXT: reference file",
        "CONTEXT: prior output",
    )

    assert projected.system_prompt
    assert projected.user_turn_content
    assert projected.channel_manifest() == {
        "system_instruction": "append-system-prompt",
        "user_task_prompt": "user-turn",
        "task_context": "user-turn",
    }

    for sentinel in system_sentinels:
        assert sentinel in projected.system_prompt
        assert sentinel not in projected.user_turn_content
    assert projected.system_prompt.endswith("SYSTEM: passthrough fragment")

    for sentinel in user_turn_sentinels:
        assert sentinel not in projected.system_prompt
        assert sentinel in projected.user_turn_content


def test_opencode_project_content_routes_system_via_message_system_field() -> None:
    projected = OpenCodeAdapter().project_content(_content())
    system_sentinels = (
        "SYSTEM: skill content",
        "SYSTEM: profile body",
        "SYSTEM: report instruction",
        "SYSTEM: agent inventory",
        "SYSTEM: completion contract",
        "SYSTEM: passthrough fragment",
    )
    user_turn_sentinels = (
        "USER: task prompt",
        "CONTEXT: reference file",
        "CONTEXT: prior output",
    )

    assert projected.system_prompt
    assert projected.user_turn_content
    assert [route.to_dict() for route in projected.reference_routing] == [
        {
            "path": "/repo/ref.txt",
            "type": "file",
            "routing": "inline",
            "native_flag": None,
        }
    ]
    assert projected.channel_manifest() == {
        "system_instruction": "system-field",
        "user_task_prompt": "user-turn",
        "task_context": "user-turn",
    }

    for sentinel in system_sentinels:
        assert sentinel in projected.system_prompt
        assert sentinel not in projected.user_turn_content
    assert projected.system_prompt.endswith("SYSTEM: passthrough fragment")

    for sentinel in user_turn_sentinels:
        assert sentinel not in projected.system_prompt
        assert sentinel in projected.user_turn_content


def test_codex_project_content_routes_system_via_developer_instructions() -> None:
    projected = CodexAdapter().project_content(_content())
    system_sentinels = (
        "SYSTEM: skill content",
        "SYSTEM: profile body",
        "SYSTEM: report instruction",
        "SYSTEM: agent inventory",
        "SYSTEM: completion contract",
        "SYSTEM: passthrough fragment",
    )
    user_turn_sentinels = (
        "USER: task prompt",
        "CONTEXT: reference file",
        "CONTEXT: prior output",
    )

    assert projected.system_prompt
    assert projected.user_turn_content
    assert projected.channel_manifest() == {
        "system_instruction": "system-field",
        "user_task_prompt": "user-turn",
        "task_context": "user-turn",
    }

    for sentinel in system_sentinels:
        assert sentinel in projected.system_prompt
        assert sentinel not in projected.user_turn_content
    assert projected.system_prompt.endswith("SYSTEM: passthrough fragment")

    for sentinel in user_turn_sentinels:
        assert sentinel not in projected.system_prompt
        assert sentinel in projected.user_turn_content


def test_claude_cli_projection_uses_system_prompt_file_and_positional_user_turn(
    tmp_path: Path,
) -> None:
    system_prompt_file = tmp_path / "system-prompt.md"
    user_turn = "USER: task prompt\n\nCONTEXT: reference file"

    command = project_claude_spec_to_cli_args(
        ClaudeLaunchSpec(
            appended_system_prompt="SYSTEM: managed prompt",
            prompt_file_path=str(system_prompt_file),
            user_turn_content=user_turn,
            interactive=True,
            extra_args=("--resume", "tail-wins"),
            permission_resolver=_resolver(),
        ),
        base_command=("claude",),
    )

    assert "--append-system-prompt-file" in command
    system_file_index = command.index("--append-system-prompt-file")
    assert command[system_file_index + 1] == str(system_prompt_file)
    assert "--append-system-prompt" not in command
    assert "SYSTEM: managed prompt" not in command
    assert command[-1] == user_turn


def test_claude_subprocess_projection_adds_stdin_sentinel_inside_projector() -> None:
    command = project_claude_spec_to_cli_args(
        ClaudeLaunchSpec(
            prompt="USER: task prompt",
            interactive=False,
            permission_resolver=_resolver(),
        ),
        base_command=("claude", "-p", "--output-format", "stream-json", "--verbose"),
    )

    assert command[:5] == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert command[5] == "-"


def test_codex_cli_projection_combines_instruction_fields_before_user_turn() -> None:
    command = project_codex_spec_to_cli_args(
        CodexLaunchSpec(
            base_instructions="SYSTEM: base",
            developer_instructions="SYSTEM: developer",
            prompt="USER: task",
            interactive=False,
            extra_args=("--skip-git-repo-check",),
            permission_resolver=_resolver(),
        ),
        base_command=("codex", "exec", "--json"),
    )

    assert command[-1] == "SYSTEM: base\n\nSYSTEM: developer\n\nUSER: task"
    _assert_ordered(
        command[-1],
        (
            "SYSTEM: base",
            "SYSTEM: developer",
            "USER: task",
        ),
    )
