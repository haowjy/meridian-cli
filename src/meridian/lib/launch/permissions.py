"""Bind-time permission facts: the platform rules and the grants they depend on."""

from __future__ import annotations

from meridian.lib.safety.policy import PlatformRule
from meridian.lib.tools import ToolRule, capability_action

# Capabilities whose native tools let an agent delegate outside Meridian's tracking.
# Sorted: the order is the order a harness's compile emits them in.
_NATIVE_SUBAGENT_CAPABILITIES: tuple[str, ...] = ("agent", "task")


def _native_subagent_capabilities(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    native_agent_copy: bool,
) -> tuple[str, ...]:
    """Return the delegation capabilities the platform denies for this launch.

    ``agent`` is denied everywhere unless native agent copies are the project's delegation
    mechanism. Spawns also deny ``task``, and ``agent`` when copies are off, unless the
    profile's own rules allow or ask for it, or the rules already deny it.
    ``profile_tool_rules`` are the profile-declared rules: only a profile can opt out, never
    a request-level rule.
    """

    capabilities: list[str] = []
    if spawn:
        for capability in _NATIVE_SUBAGENT_CAPABILITIES:
            if capability == "agent" and native_agent_copy:
                continue
            if capability_action(profile_tool_rules, capability) in {"allow", "ask"}:
                continue
            if capability_action(tool_rules, capability) == "deny":
                continue
            capabilities.append(capability)
    if not native_agent_copy and "agent" not in capabilities:
        capabilities.append("agent")
    return tuple(capabilities)


def launch_platform_rules(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    native_agent_copy: bool,
    allow_builtin_agents: bool,
) -> tuple[PlatformRule, ...]:
    """Return the platform rules for one launch; each harness names them natively."""

    rules: list[PlatformRule] = []
    capabilities = _native_subagent_capabilities(
        tool_rules=tool_rules,
        profile_tool_rules=profile_tool_rules,
        spawn=spawn,
        native_agent_copy=native_agent_copy,
    )
    if capabilities:
        rules.append(PlatformRule(kind="native-subagents", capabilities=capabilities))
    if not allow_builtin_agents:
        rules.append(PlatformRule(kind="builtin-agents"))
    return tuple(rules)


__all__ = ["launch_platform_rules"]
