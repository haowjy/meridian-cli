"""Claude's compile of the permission policy.

Tool rules and platform rules become ``--allowedTools`` / ``--disallowedTools`` entries
and the approval mode becomes Claude's permission mode. ``project_claude.py`` turns the
typed result into argv; nothing here builds argv.

Platform rules: ``native-subagents`` denies ``Agent`` / ``Task*`` and overrides the
profile's own ``agent`` rules; Claude's built-in agents are always denied, because
``allow_builtin_agents`` does not reach Claude yet (the ``builtin-agents`` rule is not read).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from meridian.lib.catalog.mars_roots import resolve_mars_roots
from meridian.lib.core.types import normalize_mars_target_name
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.tools import ToolAction

# Claude's tool names per capability. Unknown capabilities are re-capitalized, which
# misnames multi-word tools; the Mars-typed rules replace this table.
_CAPABILITY_TO_CLAUDE_TOOLS: dict[str, tuple[str, ...]] = {
    "bash": ("Bash",),
    "edit": ("Edit", "Write"),
    "write": ("Write",),
    "read": ("Read",),
    "glob": ("Glob",),
    "grep": ("Grep",),
    "list": ("LS",),
    "task": (
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    ),
    "web": ("WebSearch", "WebFetch"),
    "agent": ("Agent",),
    "external_directory": (),
    "mcp": (),
}

_BUILTIN_AGENT_PATTERNS: tuple[str, ...] = ("Explore", "Plan", "General-purpose", "general-purpose")

type _RuleKey = tuple[str, str | None]


@dataclass(frozen=True)
class ClaudePermissions:
    """What Claude is told about permissions for one launch.

    ``native_agents_enabled`` is false when Claude's ``Agent`` tool is off limits; the
    projector then also drops ``Agent`` from the allow lists it merges in (parent
    project and user passthrough).
    """

    skip_permission_prompts: bool = False
    permission_mode: Literal["acceptEdits", "default"] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    native_agents_enabled: bool = False


def is_claude_agent_tool(tool: str) -> bool:
    return tool == "Agent" or tool.startswith("Agent(")


def project_has_claude_agent_copy(project_root: Path) -> bool:
    """Return whether effective Mars config enables Claude native agent copies."""

    mars_root = resolve_mars_roots(project_root).primary
    targets: tuple[str, ...] = ()
    agent_copy_harnesses: tuple[str, ...] = ()
    for config_name in ("mars.toml", "mars.local.toml"):
        config_path = mars_root / config_name
        if not config_path.is_file():
            continue
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
        settings = cast("object", payload.get("settings"))
        if not isinstance(settings, dict):
            continue
        typed_settings = cast("dict[str, object]", settings)
        raw_targets = typed_settings.get("targets")
        if isinstance(raw_targets, list):
            targets = tuple(
                normalize_mars_target_name(str(target))
                for target in cast("list[object]", raw_targets)
            )
        raw_managed_root = typed_settings.get("managed_root")
        if not targets and isinstance(raw_managed_root, str):
            normalized_root = normalize_mars_target_name(raw_managed_root)
            if normalized_root:
                targets = (normalized_root,)
        meridian_table = typed_settings.get("meridian")
        if not isinstance(meridian_table, dict):
            continue
        agent_copy = cast("dict[str, object]", meridian_table).get("agent_copy")
        if not isinstance(agent_copy, dict) or "harnesses" not in agent_copy:
            continue
        typed_agent_copy = cast("dict[str, object]", agent_copy)
        harnesses = typed_agent_copy.get("harnesses")
        if isinstance(harnesses, list):
            agent_copy_harnesses = tuple(
                str(harness).strip().lower()
                for harness in cast("list[object]", harnesses)
            )
    return "claude" in agent_copy_harnesses and "claude" in targets


def _claude_tools_for_capability(capability: str) -> tuple[str, ...]:
    mapped = _CAPABILITY_TO_CLAUDE_TOOLS.get(capability.lower())
    if mapped is not None:
        return mapped
    if not capability:
        return ()
    return (capability[:1].upper() + capability[1:],)


def _effective_rules(spec: ResolvedLaunchSpec) -> dict[_RuleKey, ToolAction]:
    """The profile's rules with the platform's denies laid over them.

    A platform deny replaces a same-key rule in place and otherwise appends; for ``agent``
    it also overrides every scoped ``agent(...)`` rule. The built-in agents are always
    denied (see the module docstring).
    """

    policy = spec.permission_policy
    effective: dict[_RuleKey, ToolAction] = {}
    for rule in policy.rules:
        if rule.capability is not None:
            effective[(rule.capability, rule.pattern)] = rule.action
    for platform in policy.platform_rules:
        if platform.kind != "native-subagents":
            continue
        for capability in platform.capabilities:
            if capability == "agent":
                for key in effective:
                    if key[0] == "agent":
                        effective[key] = "deny"
            effective[(capability, None)] = "deny"
    for pattern in _BUILTIN_AGENT_PATTERNS:
        effective[("agent", pattern)] = "deny"
    return effective


def _compile_tool_rules(
    effective: Mapping[_RuleKey, ToolAction],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(allowed, disallowed)``; ``ask`` counts as ``allow`` (no flag expresses it)."""

    default_action = effective.get(("*", None))
    allowed: list[str] = []
    disallowed: list[str] = []
    known_all_tools = dedupe_nonempty(
        tool for tools in _CAPABILITY_TO_CLAUDE_TOOLS.values() for tool in tools
    )

    for (capability, pattern), action in effective.items():
        if capability == "*" and pattern is None:
            continue
        mapped = _claude_tools_for_capability(capability)
        if action in {"allow", "ask"}:
            target = allowed
        elif action == "deny" and default_action != "deny":
            target = disallowed
        else:
            continue
        if pattern is None:
            target.extend(mapped)
        else:
            target.extend(f"{tool}({pattern})" for tool in mapped)

    if default_action == "deny" and not allowed:
        # Best-effort representation for "deny everything".
        disallowed.extend(known_all_tools)

    return tuple(dedupe_nonempty(allowed)), tuple(dedupe_nonempty(disallowed))


def compile_claude_permissions(spec: ResolvedLaunchSpec) -> ClaudePermissions:
    """Compile ``spec.permission_policy`` for Claude."""

    policy = spec.permission_policy
    allowed, disallowed = _compile_tool_rules(_effective_rules(spec))
    if not policy.native_agent_copy:
        allowed = tuple(tool for tool in allowed if not is_claude_agent_tool(tool))
    builtin_agent_denies = tuple(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)
    return ClaudePermissions(
        skip_permission_prompts=policy.approval in ("yolo", "never"),
        permission_mode=(
            "acceptEdits"
            if policy.approval == "auto"
            else "default"
            if policy.approval == "confirm"
            else None
        ),
        allowed_tools=allowed,
        disallowed_tools=tuple(dedupe_nonempty((*disallowed, *builtin_agent_denies))),
        native_agents_enabled=policy.native_agent_copy,
    )


__all__ = [
    "ClaudePermissions",
    "compile_claude_permissions",
    "is_claude_agent_tool",
    "project_has_claude_agent_copy",
]
