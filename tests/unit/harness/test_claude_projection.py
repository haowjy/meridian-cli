from __future__ import annotations

import json

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.harness.projections.permissions_claude import compile_claude_permissions
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.launch.launch_types import ClaudeRuntimeResolution, ResolvedLaunchSpec
from meridian.lib.safety.policy import PermissionPolicy, PlatformRule, build_permission_policy
from tests.support.launch import tool_rule


def _tool_flag_entries(argv: list[str], flag: str) -> tuple[str, ...]:
    values: list[str] = []
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            values.extend(item for item in argv[index + 1].split(",") if item)
        elif token.startswith(f"{flag}="):
            values.extend(item for item in token.partition("=")[2].split(",") if item)
    return tuple(values)


def _flag_count(argv: list[str], flag: str) -> int:
    return sum(1 for token in argv if token == flag or token.startswith(f"{flag}="))


def _settings_permission_entries(argv: list[str], action: str) -> tuple[str, ...]:
    if "--settings" not in argv:
        return ()
    settings_index = max(index for index, token in enumerate(argv) if token == "--settings")
    payload = json.loads(argv[settings_index + 1])
    return tuple(payload.get("permissions", {}).get(action, ()))


def test_claude_projection_drops_inherited_agent_when_native_agents_disabled() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            platform_rules=(PlatformRule(kind="native-subagents", capabilities=("agent",)),)
        ),
        extra_args=(
            CLAUDE_PARENT_ALLOWED_TOOLS_FLAG,
            "Agent,Agent(custom),Bash(meridian spawn *)",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == ("Bash(meridian spawn *)",)


def test_claude_projection_keeps_inherited_agent_when_native_agents_enabled() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(),
        extra_args=(
            CLAUDE_PARENT_ALLOWED_TOOLS_FLAG,
            "Agent,Agent(custom),Bash(meridian spawn *)",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == (
        "Agent",
        "Agent(custom)",
        "Bash(meridian spawn *)",
    )


def test_claude_projection_passthrough_allowed_tools_cannot_readd_denied_agent() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(tool_rule("agent", "deny", "Agent"), tool_rule("bash", "allow", "Bash")),
            sandbox=None,
        ),
        extra_args=("--allowedTools", "Agent,Bash(meridian spawn *)"),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == (
        "Bash",
        "Bash(meridian spawn *)",
    )
    assert "Agent" in _tool_flag_entries(command, "--disallowedTools")


def test_claude_projection_strips_raw_tool_flags_from_passthrough() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(
                tool_rule("bash", "allow", "Bash"),
                tool_rule("web", "ask", "WebSearch", pattern="query"),
                tool_rule("agent", "deny", "Agent"),
            ),
            sandbox=None,
        ),
        extra_args=(
            "--allowedTools=Agent,Agent(custom),Bash(meridian spawn *)",
            "--disallowedTools",
            "Write",
            "--settings",
            '{"permissions":{"allow":["Agent"]}}',
            "--verbose",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    allowed = _settings_permission_entries(command, "allow")
    disallowed = _tool_flag_entries(command, "--disallowedTools")
    assert allowed == ("Bash", "Bash(meridian spawn *)")
    assert _settings_permission_entries(command, "ask") == ("WebSearch",)
    assert "WebSearch" not in allowed
    assert "Agent" in disallowed
    assert "Write" in disallowed
    assert "--verbose" in command
    assert _flag_count(command, "--allowedTools") == 0
    assert _flag_count(command, "--disallowedTools") == 1
    assert command.index("--settings") < command.index("--disallowedTools")
    _, report = compile_claude_permissions(spec)
    external = next(
        line
        for line in report
        if line.element == "source: Claude parent-project and passthrough tool allows"
    )
    assert external.status == "partial"
    assert "Bash(meridian spawn *)" in external.note
    assert "Agent" in external.note


def test_claude_projection_default_deny_agent_allow_still_denies_builtins() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(tool_rule("*", "deny"), tool_rule("agent", "allow", "Agent")),
            sandbox="workspace-write",
            approval="auto",
            platform_rules=(PlatformRule(kind="builtin-agents"),),
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _tool_flag_entries(command, "--tools") == ("Agent",)
    assert _settings_permission_entries(command, "allow") == ("Agent",)
    disallowed = _tool_flag_entries(command, "--disallowedTools")
    assert "Agent" not in disallowed
    assert "Agent(Explore)" in disallowed
    assert "Agent(Plan)" in disallowed
    assert "Agent(General-purpose)" in disallowed
    assert "Agent(general-purpose)" in disallowed
    settings_index = max(index for index, token in enumerate(command) if token == "--settings")
    settings = json.loads(command[settings_index + 1])
    assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False


@pytest.mark.parametrize("continuation", [False, True])
def test_interactive_prompt_is_unconsumable_after_passthrough(continuation: bool) -> None:
    prompt = "Reply exactly: hello"
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        interactive=True,
        user_turn_content=prompt,
        continue_session_id="claude-session" if continuation else None,
        extra_args=("--add-dir", "/tmp"),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert command[-2:] == ["--", prompt]
    if continuation:
        assert command[command.index("--resume") + 1] == "claude-session"
    else:
        assert "--resume" not in command


@pytest.mark.parametrize("interactive", [False, True])
def test_claude_projection_loads_selected_plugin_dirs(
    interactive: bool,
) -> None:
    plugin_dirs = ("/plugins/first", "/plugins/second")
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        interactive=interactive,
        claude_runtime=ClaudeRuntimeResolution(plugin_dirs=plugin_dirs),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    projected_dirs = tuple(
        command[index + 1]
        for index, token in enumerate(command)
        if token == "--plugin-dir"
    )
    assert projected_dirs == plugin_dirs
