"""Bind-time permission facts: the platform rules and the grants they depend on."""

from __future__ import annotations

from meridian.lib.safety.policy import PlatformCommand, PlatformRule
from meridian.lib.tools import (
    PLATFORM_ORIGIN,
    ToolRule,
    legacy_platform_tool_rules,
    unscoped_action,
)


def _exact(command: str) -> PlatformCommand:
    return PlatformCommand(command=command, match="exact")


def _prefix(command: str) -> PlatformCommand:
    return PlatformCommand(command=command, match="prefix")


# What a child runs inside itself to answer Meridian's completion confirmation (the inherited
# spawn id targets it). A child without these commands cannot reject a premature completion; it
# closes only when its completion window runs out.
_COMPLETION_COMMANDS: tuple[PlatformCommand, ...] = (
    _prefix("meridian spawn done"),
    _prefix("meridian spawn rearm"),
)

# Commands whose registered behavior reads or reports without mutating project/runtime authority.
# Keep mixed surfaces exact (task-dir and work task-dir) so their mutation forms remain governed by
# the profile. CLI help used by the injected guidance is listed explicitly for the same reason.
_READ_ONLY_COMMANDS: tuple[PlatformCommand, ...] = (
    _exact("meridian -h"),
    _exact("meridian --help"),
    _exact("meridian -v"),
    _exact("meridian --version"),
    _exact("meridian spawn -h"),
    _exact("meridian spawn --help"),
    _exact("meridian session -h"),
    _exact("meridian session --help"),
    _exact("meridian work -h"),
    _exact("meridian work --help"),
    _exact("meridian context -h"),
    _exact("meridian context --help"),
    _prefix("meridian artifact list"),
    _prefix("meridian completion bash"),
    _prefix("meridian completion fish"),
    _prefix("meridian completion zsh"),
    _prefix("meridian config get"),
    _prefix("meridian config show"),
    _prefix("meridian context"),
    _prefix("meridian ext commands"),
    _prefix("meridian ext list"),
    _prefix("meridian ext show"),
    _prefix("meridian hooks check"),
    _prefix("meridian hooks list"),
    _prefix("meridian idle config"),
    _prefix("meridian idle mod-path"),
    _prefix("meridian idle status"),
    _prefix("meridian kg check"),
    _prefix("meridian kg graph"),
    _prefix("meridian mermaid check"),
    _prefix("meridian qi check"),
    _prefix("meridian qi graph"),
    _prefix("meridian qi list"),
    _prefix("meridian session export"),
    _prefix("meridian session log"),
    _prefix("meridian session search"),
    _prefix("meridian spawn children"),
    _prefix("meridian spawn list"),
    _prefix("meridian spawn report search"),
    _prefix("meridian spawn report show"),
    _prefix("meridian spawn show"),
    _prefix("meridian spawn stats"),
    _prefix("meridian spawn status"),
    _prefix("meridian spawn subagents"),
    _prefix("meridian sync conflict list"),
    _prefix("meridian sync conflict show"),
    _exact("meridian task-dir"),
    _prefix("meridian telemetry query"),
    _prefix("meridian telemetry status"),
    _prefix("meridian telemetry tail"),
    _exact("meridian work"),
    _prefix("meridian work current"),
    _prefix("meridian work list"),
    _prefix("meridian work root"),
    _prefix("meridian work sessions"),
    _prefix("meridian work show"),
    _exact("meridian work task-dir"),
)

_SPAWN_COMMANDS: tuple[PlatformCommand, ...] = (_prefix("meridian spawn"),)


def _native_subagent_capabilities(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    native_agent_copy: bool,
) -> tuple[str, ...]:
    """Return the delegation capabilities the platform denies for this launch.

    ``agent`` is denied on every launch unless native agent copies are the project's
    delegation mechanism; no profile rule opts out of it. Spawns also deny ``task`` unless the
    profile has an explicit unscoped ``task: allow`` or ``ask`` (``*: allow`` does not count:
    it says nothing about delegation), or an unscoped rule already denies it. A ``*: deny``
    does not stand in for that rule: a platform deny is a rule of its own, so it holds under
    an allowlist too. ``profile_tool_rules`` are the profile-declared rules: only a profile
    can opt out, never a request-level rule.
    """

    capabilities: list[str] = []
    if not native_agent_copy:
        capabilities.append("agent")
    if (
        spawn
        and unscoped_action(profile_tool_rules, "task") not in {"allow", "ask"}
        and unscoped_action(tool_rules, "task") != "deny"
    ):
        capabilities.append("task")
    return tuple(capabilities)


def launch_permission_rules(
    *,
    tool_rules: tuple[ToolRule, ...],
    profile_tool_rules: tuple[ToolRule, ...],
    platform_tool_rules: tuple[ToolRule, ...],
    spawn: bool,
    has_spawn_capability: bool,
    native_agent_copy: bool,
    allow_builtin_agents: bool,
) -> tuple[tuple[ToolRule, ...], tuple[PlatformRule, ...]]:
    """Select ordinary platform denies and the remaining mechanism rules for a launch.

    Every launch gets Meridian's completion and read commands. A launch whose profile can
    spawn also gets the spawn command prefix. A harness's own defaults can close the shell to
    a headless child even without tool rules, so these grants cannot depend on the profile's
    shell rules. A compile whose shell is already open has nothing to add for them.
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
    commands = (*_COMPLETION_COMMANDS, *_READ_ONLY_COMMANDS)
    if has_spawn_capability:
        commands = (*commands, *_SPAWN_COMMANDS)
    rules.append(PlatformRule(kind="meridian-commands", commands=commands))
    return denies, tuple(rules)


__all__ = ["launch_permission_rules"]
