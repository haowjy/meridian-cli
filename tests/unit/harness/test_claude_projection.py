from __future__ import annotations

import json

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.permissions_claude import (
    compile_claude_permissions,
    project_claude_network,
)
from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
from meridian.lib.launch.launch_types import ClaudeRuntimeResolution, ResolvedLaunchSpec
from meridian.lib.launch.permissions import SPAWN_PLATFORM_COMMAND
from meridian.lib.safety.policy import (
    Allow,
    Hosts,
    PermissionPolicy,
    PlatformCommand,
    PlatformRule,
    build_permission_policy,
    network_policy_name,
)
from meridian.lib.tools import PLATFORM_ORIGIN
from tests.support.launch import tool_rule

_STRICT_OFFLINE_NETWORK = {
    "strictAllowlist": True,
    "allowLocalBinding": False,
    "allowAllUnixSockets": False,
}


def _spec(**values: object) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec.model_validate(values)


def _effective_network(spec: ResolvedLaunchSpec) -> str:
    projection = project_claude_network(
        spec.permission_policy,
        native_config=spec.native_config,
    )
    return network_policy_name(projection.effective)


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


def test_claude_projection_drops_passthrough_agent_when_native_agents_disabled() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            rules=(
                tool_rule("agent", "deny", "Agent").model_copy(update={"origin": PLATFORM_ORIGIN}),
            )
        ),
        extra_args=("--allowedTools", "Agent,Agent(custom),Bash(meridian spawn *)"),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == ("Bash(meridian spawn *)",)


def test_claude_projection_keeps_passthrough_agent_when_native_agents_enabled() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(),
        extra_args=("--allowedTools", "Agent,Agent(custom),Bash(meridian spawn *)"),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == (
        "Agent",
        "Agent(custom)",
        "Bash(meridian spawn *)",
    )


def test_claude_projection_passthrough_allowed_tools_cannot_readd_denied_agent() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(tool_rule("agent", "deny", "Agent"), tool_rule("bash", "allow", "Bash")),
            sandbox=None,
            network=Allow(),
        ),
        extra_args=("--allowedTools", "Agent,Bash(meridian spawn *)"),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == (
        "Bash",
        "Bash(meridian spawn *)",
    )
    assert "Agent" in _tool_flag_entries(command, "--disallowedTools")


def test_claude_projection_spells_exact_and_prefix_meridian_commands() -> None:
    platform = PlatformRule(
        kind="meridian-commands",
        commands=(
            PlatformCommand(command="meridian task-dir", match="exact"),
            PlatformCommand(command="meridian spawn list", match="prefix"),
        ),
    )
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(platform_rules=(platform,)),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    assert _settings_permission_entries(command, "allow") == (
        "Bash(meridian task-dir)",
        "Bash(meridian spawn list)",
        "Bash(meridian spawn list *)",
    )
    assert "Bash(meridian task-dir *)" not in _settings_permission_entries(command, "allow")
    _, report = compile_claude_permissions(spec)
    line = next(item for item in report if item.element.startswith("platform: allow Meridian"))
    assert "exact: meridian task-dir" in line.element
    assert "prefix: meridian spawn list" in line.element


def test_claude_meridian_report_distinguishes_command_precedence() -> None:
    platform = PlatformRule(
        kind="meridian-commands",
        commands=(
            PlatformCommand(command="meridian spawn", match="prefix", role="launch"),
            PlatformCommand(
                command="meridian spawn done",
                match="prefix",
                role="completion",
            ),
        ),
    )
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            rules=(
                tool_rule(
                    "bash",
                    "deny",
                    "Bash(meridian spawn cancel-all *)",
                    pattern="meridian spawn cancel-all *",
                ),
                tool_rule(
                    "bash",
                    "deny",
                    "Bash(meridian spawn done private *)",
                    pattern="meridian spawn done private *",
                ),
            ),
            platform_rules=(platform,),
        ),
    )

    permissions, report = compile_claude_permissions(spec)

    assert "Bash(meridian spawn cancel-all *)" in permissions.settings.deny
    line = next(item for item in report if item.element.startswith("platform: allow Meridian"))
    assert line.status == "partial"
    assert "Bash(meridian spawn cancel-all *)" in line.note
    assert "cannot keep the completion grant final" in line.note


def test_claude_projection_defaults_sandboxed_network_to_deny() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            platform_rules=(
                PlatformRule(kind="meridian-commands", commands=(SPAWN_PLATFORM_COMMAND,)),
            ),
            sandbox="workspace-write",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["sandbox"]["excludedCommands"] == ["meridian spawn *"]
    assert settings["sandbox"]["network"] == _STRICT_OFFLINE_NETWORK
    _, report = compile_claude_permissions(spec)
    sandbox_line = next(item for item in report if item.element == "sandbox: workspace-write")
    assert "runs fully outside the sandbox" in sandbox_line.note
    network_line = next(item for item in report if item.element == "network: deny")
    assert network_line.status == "partial"
    assert _effective_network(spec) == "deny"


def test_claude_network_report_stays_on_meridian_projected_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("meridian.lib.harness.projections.network_claude.sys.platform", "linux")
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            sandbox="workspace-write",
            network="deny",
            platform_rules=(
                PlatformRule(
                    kind="meridian-commands",
                    commands=(SPAWN_PLATFORM_COMMAND,),
                ),
            ),
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    settings = json.loads(command[command.index("--settings") + 1])

    assert settings["sandbox"]["network"] == _STRICT_OFFLINE_NETWORK
    _, report = compile_claude_permissions(spec)
    line = next(item for item in report if item.element == "network: deny")
    assert line.status == "partial"
    assert _effective_network(spec) == "deny"


def test_claude_network_deny_holds_for_allowlist_with_approval_never() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(tool_rule("*", "deny"), tool_rule("bash", "allow", "Bash")),
            sandbox="workspace-write",
            network=Hosts(),
            approval="never",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    settings = json.loads(command[command.index("--settings") + 1])

    assert settings["sandbox"]["network"] == _STRICT_OFFLINE_NETWORK
    assert "--dangerously-skip-permissions" not in command
    assert _effective_network(spec) == "deny"


def test_claude_typed_deny_policy_projects_network_settings() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            sandbox="workspace-write",
            network="deny",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    settings = json.loads(command[command.index("--settings") + 1])
    _, report = compile_claude_permissions(spec)

    assert settings["sandbox"]["network"] == _STRICT_OFFLINE_NETWORK
    assert _effective_network(spec) == "deny"
    assert any(line.element == "network: deny" for line in report)


def test_claude_allowlist_translates_double_wildcards_and_gates_local_switches() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            sandbox="workspace-write",
            network={
                "allow": ["**.github.com", "localhost"],
                "deny": ["**.blocked.example"],
            },
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    settings = json.loads(command[command.index("--settings") + 1])
    network = settings["sandbox"]["network"]
    _, report = compile_claude_permissions(spec)
    line = next(item for item in report if item.element.startswith("network: allowlist"))

    assert network == {
        "strictAllowlist": True,
        "allowedDomains": ["github.com", "*.github.com", "localhost"],
        "deniedDomains": ["blocked.example", "*.blocked.example"],
        "allowLocalBinding": True,
        "allowAllUnixSockets": True,
    }
    assert "allow=2 [**.github.com, localhost]" in line.element
    assert "deny=1 [**.blocked.example]" in line.element
    assert line.status == "partial"


def test_claude_projection_does_not_escape_user_configured_default_sandbox() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            platform_rules=(
                PlatformRule(kind="meridian-commands", commands=(SPAWN_PLATFORM_COMMAND,)),
            )
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    settings = json.loads(command[command.index("--settings") + 1])
    assert "sandbox" not in settings


def test_claude_projection_does_not_exclude_spawn_when_sandbox_is_disabled() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=PermissionPolicy(
            platform_rules=(
                PlatformRule(kind="meridian-commands", commands=(SPAWN_PLATFORM_COMMAND,)),
            ),
            sandbox="danger-full-access",
        ),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["sandbox"] == {"enabled": False}


def test_claude_projection_strips_raw_tool_flags_from_passthrough() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(
                tool_rule("bash", "allow", "Bash"),
                tool_rule("web", "ask", "WebSearch", pattern="query"),
                tool_rule("agent", "deny", "Agent"),
            ),
            sandbox=None,
            network=Allow(),
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
    external = next(line for line in report if line.element == "source: passthrough --allowedTools")
    assert external.status == "partial"
    assert "Bash(meridian spawn *)" in external.note
    assert "Agent" in external.note


def test_claude_projection_default_deny_agent_allow_still_denies_builtins() -> None:
    spec = _spec(
        harness=HarnessId.CLAUDE,
        prompt="test",
        permission_policy=build_permission_policy(
            rules=(tool_rule("*", "deny"), tool_rule("agent", "allow", "Agent")),
            sandbox="workspace-write",
            network=Hosts(),
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
    spec = _spec(
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
    spec = _spec(
        harness=HarnessId.CLAUDE,
        interactive=interactive,
        claude_runtime=ClaudeRuntimeResolution(plugin_dirs=plugin_dirs),
    )

    command = project_claude_spec_to_cli_args(spec, base_command=("claude",))

    projected_dirs = tuple(
        command[index + 1] for index, token in enumerate(command) if token == "--plugin-dir"
    )
    assert projected_dirs == plugin_dirs
