from __future__ import annotations

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
    ToolsPermissionResolver,
)


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


def test_claude_projection_drops_inherited_agent_when_native_agents_disabled() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        extra_args=(
            CLAUDE_PARENT_ALLOWED_TOOLS_FLAG,
            "Agent,Agent(custom),Bash(meridian spawn *)",
        ),
        claude_native_agents_enabled=False,
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _tool_flag_entries(command, "--allowedTools") == ("Bash(meridian spawn *)",)


def test_claude_projection_keeps_inherited_agent_when_native_agents_enabled() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        extra_args=(
            CLAUDE_PARENT_ALLOWED_TOOLS_FLAG,
            "Agent,Agent(custom),Bash(meridian spawn *)",
        ),
        claude_native_agents_enabled=True,
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _tool_flag_entries(command, "--allowedTools") == (
        "Agent",
        "Agent(custom)",
        "Bash(meridian spawn *)",
    )


def test_claude_projection_passthrough_allowed_tools_cannot_readd_denied_agent() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_resolver=ToolsPermissionResolver(
            tools={"agent": "deny", "bash": "allow"},
            fallback_config=PermissionConfig(),
        ),
        extra_args=("--allowedTools", "Agent,Bash(meridian spawn *)"),
        claude_native_agents_enabled=False,
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _tool_flag_entries(command, "--allowedTools") == (
        "Bash",
        "Bash(meridian spawn *)",
    )
    assert "Agent" in _tool_flag_entries(command, "--disallowedTools")


def test_claude_projection_strips_raw_tool_flags_from_passthrough() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_resolver=ToolsPermissionResolver(
            tools={"bash": "allow", "agent": "deny"},
            fallback_config=PermissionConfig(),
        ),
        extra_args=(
            "--allowedTools=Agent,Agent(custom),Bash(meridian spawn *)",
            "--disallowedTools",
            "Write",
            "--verbose",
        ),
        claude_native_agents_enabled=False,
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    allowed = _tool_flag_entries(command, "--allowedTools")
    disallowed = _tool_flag_entries(command, "--disallowedTools")
    assert allowed == ("Bash", "Bash(meridian spawn *)")
    assert "Agent" in disallowed
    assert "Write" in disallowed
    assert "--verbose" in command
    assert _flag_count(command, "--allowedTools") == 1
    assert _flag_count(command, "--disallowedTools") == 1


def test_claude_projection_default_deny_agent_allow_still_denies_builtins() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_resolver=ToolsPermissionResolver(
            tools={"*": "deny", "agent": "allow"},
            fallback_config=PermissionConfig(),
        ),
        claude_native_agents_enabled=True,
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _tool_flag_entries(command, "--allowedTools") == ("Agent",)
    disallowed = _tool_flag_entries(command, "--disallowedTools")
    assert "Agent" not in disallowed
    assert "Agent(Explore)" in disallowed
    assert "Agent(Plan)" in disallowed
    assert "Agent(General-purpose)" in disallowed
    assert "Agent(general-purpose)" in disallowed
