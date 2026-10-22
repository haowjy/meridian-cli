"""Claude's compile of the permission policy.

Tool rules and platform rules become ``--allowedTools`` / ``--disallowedTools`` entries
and the approval mode becomes Claude's permission mode. ``project_claude.py`` turns the
typed result into argv; nothing here builds argv. The compile also reports what it did
with each element of the policy.

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
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    PlatformRule,
    ReportLine,
    ToolRule,
    complete_report,
    platform_element,
    rule_element,
)
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
                str(harness).strip().lower() for harness in cast("list[object]", harnesses)
            )
    return "claude" in agent_copy_harnesses and "claude" in targets


def _claude_tools_for_capability(capability: str) -> tuple[str, ...]:
    mapped = _CAPABILITY_TO_CLAUDE_TOOLS.get(capability.lower())
    if mapped is not None:
        return mapped
    if not capability:
        return ()
    return (capability[:1].upper() + capability[1:],)


def _effective_rules(
    spec: ResolvedLaunchSpec,
) -> tuple[dict[_RuleKey, ToolAction], frozenset[_RuleKey]]:
    """The profile's rules with the platform's denies laid over them.

    A platform deny replaces a same-key rule in place and otherwise appends; for ``agent``
    it also overrides every scoped ``agent(...)`` rule. The built-in agents are always
    denied (see the module docstring). Also returns the profile rules a platform deny
    changed.
    """

    policy = spec.permission_policy
    stated: dict[_RuleKey, ToolAction] = {}
    for rule in policy.rules:
        if rule.capability is not None:
            stated[(rule.capability, rule.pattern)] = rule.action
    effective = dict(stated)
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
    overridden = frozenset(key for key, action in stated.items() if effective[key] != action)
    return effective, overridden


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


def _tool_rule_line(
    rule: ToolRule, allowed: frozenset[str], disallowed: frozenset[str], *, has_allows: bool
) -> ReportLine:
    element = rule_element(rule)
    capability = rule.capability or ""
    if capability == "*" and rule.pattern is None:
        if rule.action != "deny":
            return ReportLine(element, "unsupported", "no flag; Claude's own default decides")
        if has_allows:
            return ReportLine(
                element,
                "unsupported",
                "not emitted: only the allow rules reach Claude, so unlisted tools stay available",
            )
        return ReportLine(
            element,
            "partial",
            "emitted as a deny of every tool Meridian names; MCP and other tools stay available",
        )
    mapped = _claude_tools_for_capability(capability)
    names = mapped if rule.pattern is None else tuple(f"{tool}({rule.pattern})" for tool in mapped)
    if not names:
        return ReportLine(element, "unsupported", "no Claude tool name for this capability")
    emitted = disallowed if rule.action == "deny" else allowed
    if not all(name in emitted for name in names):
        why = (
            "`*: deny` stands in for it"
            if rule.action == "deny"
            else "Agent grants need native agent copy"
            if capability == "agent"
            else "no flag carries it"
        )
        return ReportLine(element, "unsupported", f"not emitted: {why}")
    flag = "--disallowedTools" if rule.action == "deny" else "--allowedTools"
    note = f"{flag} {','.join(names)}"
    raw = rule.native[0] if rule.native else None
    if capability not in _CAPABILITY_TO_CLAUDE_TOOLS and names != (raw,):
        return ReportLine(
            element,
            "partial",
            f"{note}; spelling differs from the bundle's {raw}, so Claude may not match it",
        )
    if rule.action == "ask":
        return ReportLine(
            element, "partial", f"{note}; Claude has no ask flag, so ask is sent as allow"
        )
    if rule.action == "allow":
        return ReportLine(element, "native", f"{note} (skips the prompt only)")
    return ReportLine(element, "native", note)


def _platform_lines(policy: PermissionPolicy) -> list[ReportLine]:
    lines = [
        ReportLine(
            platform_element(platform),
            "platform",
            "--disallowedTools "
            + ",".join(
                tool
                for capability in platform.capabilities
                for tool in _claude_tools_for_capability(capability)
            ),
        )
        for platform in policy.platform_rules
        if platform.kind == "native-subagents"
    ]
    # ``allow_builtin_agents`` does not reach Claude: the denies apply even without the rule.
    lines.append(
        ReportLine(
            platform_element(PlatformRule(kind="builtin-agents")),
            "platform",
            "--disallowedTools "
            + ",".join(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)
            + "; always, whatever allow_builtin_agents says",
        )
    )
    return lines


def _claude_report(
    policy: PermissionPolicy,
    permissions: ClaudePermissions,
    effective: Mapping[_RuleKey, ToolAction],
    overridden: frozenset[_RuleKey],
) -> EnforcementReport:
    allowed = frozenset(permissions.allowed_tools)
    disallowed = frozenset(permissions.disallowed_tools)
    has_allows = any(
        action in {"allow", "ask"} for key, action in effective.items() if key != ("*", None)
    )
    lines: list[ReportLine] = []
    for rule in policy.rules:
        if rule.capability is None:
            continue
        key = (rule.capability, rule.pattern)
        if key in overridden:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "platform",
                    f"overridden to {effective[key]} by a platform rule",
                )
            )
        else:
            lines.append(_tool_rule_line(rule, allowed, disallowed, has_allows=has_allows))
    lines.extend(_platform_lines(policy))
    if permissions.skip_permission_prompts:
        approval_note = "--dangerously-skip-permissions"
    elif permissions.permission_mode is not None:
        approval_note = f"--permission-mode {permissions.permission_mode}"
    else:
        approval_note = ""
    if approval_note:
        lines.append(ReportLine(f"approval: {policy.approval}", "native", approval_note))
    return complete_report(policy, lines, harness="Claude")


def compile_claude_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[ClaudePermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Claude, with the report of what it did."""

    policy = spec.permission_policy
    effective, overridden = _effective_rules(spec)
    allowed, disallowed = _compile_tool_rules(effective)
    builtin_agent_denies = tuple(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)
    permissions = ClaudePermissions(
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
        # Bind denies ``agent`` natively exactly when native agent copies are off.
        native_agents_enabled=not any(
            rule.kind == "native-subagents" and "agent" in rule.capabilities
            for rule in policy.platform_rules
        ),
    )
    return permissions, _claude_report(policy, permissions, effective, overridden)


__all__ = [
    "ClaudePermissions",
    "compile_claude_permissions",
    "is_claude_agent_tool",
    "project_has_claude_agent_copy",
]
