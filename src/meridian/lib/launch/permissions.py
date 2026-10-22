"""Bind-time permission facts: the platform rules and the grants they depend on."""

from __future__ import annotations

from meridian.lib.safety.policy import PlatformRule
from meridian.lib.tools import ToolsField, resolve_tool_action

# Capabilities whose native tools let an agent delegate outside Meridian's tracking.
# Sorted: the order is the order a harness's compile emits them in.
_NATIVE_SUBAGENT_CAPABILITIES: tuple[str, ...] = ("agent", "task")


def native_subagent_capabilities(
    *,
    tools: ToolsField | None,
    profile_tools: ToolsField | None,
    spawn: bool,
    native_agent_copy: bool,
) -> tuple[str, ...]:
    """Return the delegation capabilities the platform denies for this launch.

    ``agent`` is denied everywhere unless native agent copies are the project's delegation
    mechanism. Spawns also deny ``task``, and ``agent`` when copies are off, unless the
    profile's own ``tools`` allows or asks for it, or ``tools`` already denies it.
    ``profile_tools`` is the profile-declared map: only a profile can opt out, never a
    request-level ``tools``.
    """

    capabilities: list[str] = []
    if spawn:
        for capability in _NATIVE_SUBAGENT_CAPABILITIES:
            if capability == "agent" and native_agent_copy:
                continue
            if resolve_tool_action(tools=profile_tools, capability=capability) in {"allow", "ask"}:
                continue
            if resolve_tool_action(tools=tools, capability=capability) == "deny":
                continue
            capabilities.append(capability)
    if not native_agent_copy and "agent" not in capabilities:
        capabilities.append("agent")
    return tuple(capabilities)


def launch_platform_rules(
    *,
    tools: ToolsField | None,
    profile_tools: ToolsField | None,
    spawn: bool,
    native_agent_copy: bool,
    allow_builtin_agents: bool,
) -> tuple[PlatformRule, ...]:
    """Return the platform rules for one launch; each harness names them natively."""

    rules: list[PlatformRule] = []
    capabilities = native_subagent_capabilities(
        tools=tools,
        profile_tools=profile_tools,
        spawn=spawn,
        native_agent_copy=native_agent_copy,
    )
    if capabilities:
        rules.append(PlatformRule(kind="native-subagents", capabilities=capabilities))
    if not allow_builtin_agents:
        rules.append(PlatformRule(kind="builtin-agents"))
    return tuple(rules)


__all__ = [
    "launch_platform_rules",
    "native_subagent_capabilities",
]
