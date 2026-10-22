"""Bind-time permission facts: the platform rules and the grants they depend on."""

from __future__ import annotations

from meridian.lib.safety.policy import PlatformRule
from meridian.lib.tools import (
    PLATFORM_ORIGIN,
    ToolRule,
    capability_action,
    legacy_platform_tool_rules,
    unscoped_action,
)

# Capabilities whose native tools let an agent delegate outside Meridian's tracking.
# Sorted: the order is the order a harness's compile emits them in.
_NATIVE_SUBAGENT_CAPABILITIES: tuple[str, ...] = ("agent", "task")

# What a child runs inside itself to answer Meridian's completion confirmation (the inherited
# spawn id targets it). Without them a child cannot reject a premature completion; it closes
# only when its completion window runs out.
_COMPLETION_COMMANDS: tuple[str, ...] = ("meridian spawn done", "meridian spawn rearm")


def _native_subagent_capabilities(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    native_agent_copy: bool,
) -> tuple[str, ...]:
    """Return the delegation capabilities the platform denies for this launch.

    ``agent`` is denied everywhere and spawns also deny ``task``, unless native agent copies
    are the project's delegation mechanism, the profile's own rules allow or ask for the
    capability, or a rule naming it already denies it. A ``*: deny`` does not stand in for
    that rule: a platform deny is a rule of its own, so it holds under an allowlist too.
    ``profile_tool_rules`` are the profile-declared rules: only a profile can opt out, never
    a request-level rule.
    """

    capabilities: list[str] = []
    for capability in _NATIVE_SUBAGENT_CAPABILITIES:
        if capability == "task" and not spawn:
            continue
        if capability == "agent" and native_agent_copy:
            continue
        if capability_action(profile_tool_rules, capability) in {"allow", "ask"}:
            continue
        if unscoped_action(tool_rules, capability) == "deny":
            continue
        capabilities.append(capability)
    return tuple(capabilities)


def launch_permission_rules(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    platform_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    native_agent_copy: bool,
    allow_builtin_agents: bool,
) -> tuple[tuple[ToolRule, ...], tuple[PlatformRule, ...]]:
    """Select ordinary platform denies and the remaining mechanism rules for a launch.

    Every spawn gets the completion-command allow. A harness's own defaults can close the
    shell to a headless child even without tool rules (Claude asks before any command that
    changes files, and a spawn has no one to answer), so the rule cannot depend on the
    profile. A compile whose shell is already open has nothing to add for it.
    """

    capabilities = _native_subagent_capabilities(
        tool_rules=tool_rules,
        profile_tool_rules=profile_tool_rules,
        spawn=spawn,
        native_agent_copy=native_agent_copy,
    )
    candidates = platform_tool_rules or legacy_platform_tool_rules()
    denies = tuple(
        rule.model_copy(update={"action": "deny", "origin": PLATFORM_ORIGIN})
        for rule in candidates
        if rule.capability in capabilities
    )

    rules: list[PlatformRule] = []
    if not allow_builtin_agents:
        rules.append(PlatformRule(kind="builtin-agents"))
    if spawn:
        rules.append(PlatformRule(kind="completion-commands", commands=_COMPLETION_COMMANDS))
    return denies, tuple(rules)


__all__ = ["launch_permission_rules"]
