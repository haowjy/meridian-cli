"""Claude's compile of the permission policy.

Tool rules and platform rules become ``--allowedTools`` / ``--disallowedTools`` entries
and the approval mode becomes Claude's permission mode. ``project_claude.py`` turns the
typed result into argv; nothing here builds argv. The compile also reports what it did
with each element of the policy.

A tool rule is emitted by the native names Mars resolved for Claude; the compile reads a
rule's ``capability`` only to apply platform rules and the ``*`` default.

Platform rules: ``native-subagents`` denies ``Agent`` / ``Task*`` and overrides the
profile's own ``agent`` rules; Claude's built-in agents are always denied, because
``allow_builtin_agents`` does not reach Claude yet (the ``builtin-agents`` rule is not read).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    PlatformRule,
    ReportLine,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule

# What each capability of the ``native-subagents`` platform rule denies on Claude.
_PLATFORM_DENY_TOOLS: dict[str, tuple[str, ...]] = {
    "agent": ("Agent",),
    "task": ("TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"),
}

_BUILTIN_AGENT_PATTERNS: tuple[str, ...] = ("Explore", "Plan", "General-purpose", "general-purpose")

# Stands in for ``*: deny`` when the profile allows nothing: Claude has no flag for
# "everything else", so the tools Meridian knows by name are denied one by one.
_WILDCARD_DENY_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "WebSearch",
    "WebFetch",
    "Agent",
)


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


@dataclass(frozen=True)
class _Effective:
    """A profile rule with the platform's denies laid over it."""

    rule: ToolRule
    action: ToolAction

    @property
    def overridden(self) -> bool:
        return self.action != self.rule.action


def _platform_denied_capabilities(policy: PermissionPolicy) -> tuple[str, ...]:
    return tuple(
        capability
        for platform in policy.platform_rules
        if platform.kind == "native-subagents"
        for capability in platform.capabilities
    )


def _effective_rules(policy: PermissionPolicy) -> tuple[_Effective, ...]:
    """The profile's rules with the platform's denies laid over them.

    ``native-subagents`` turns every ``agent`` rule (scoped or not) and the unscoped
    ``task`` rule into a deny. The built-in agents are always denied (see the module
    docstring): their denies are added in ``compile_claude_permissions`` and a profile rule
    naming one is overridden.
    """

    denied = _platform_denied_capabilities(policy)
    effective: list[_Effective] = []
    for rule in policy.rules:
        action = rule.action
        if (
            (rule.capability == "agent" and "agent" in denied)
            or (rule.capability == "task" and rule.pattern is None and "task" in denied)
            or (rule.capability == "agent" and rule.pattern in _BUILTIN_AGENT_PATTERNS)
        ):
            action = "deny"
        effective.append(_Effective(rule, action))
    return tuple(effective)


def _compile_tool_rules(
    policy: PermissionPolicy, effective: tuple[_Effective, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(allowed, disallowed)``; ``ask`` counts as ``allow`` (no flag expresses it).

    Every deny is emitted, under ``*: deny`` too: Claude has no flag for "everything else",
    so the default cannot stand in for a deny of a named tool, and the platform's denies
    (sub-agents) must hold whatever the profile allows.
    """

    entries: list[tuple[tuple[str, ...], ToolAction]] = [
        (entry.rule.native, entry.action)
        for entry in effective
        if not entry.rule.is_wildcard and entry.rule.native_status == "known"
    ]
    entries.extend(
        (_PLATFORM_DENY_TOOLS[capability], "deny")
        for capability in _platform_denied_capabilities(policy)
    )

    allowed: list[str] = []
    disallowed: list[str] = []
    for names, action in entries:
        (disallowed if action == "deny" else allowed).extend(names)

    if policy.is_allowlist and not allowed:
        # Best-effort representation for "deny everything".
        disallowed.extend(_WILDCARD_DENY_TOOLS)

    return tuple(dedupe_nonempty(allowed)), tuple(dedupe_nonempty(disallowed))


def _tool_rule_line(entry: _Effective, allowed: frozenset[str]) -> ReportLine:
    rule = entry.rule
    element = rule_element(rule)
    if entry.overridden:
        return ReportLine(element, "platform", f"overridden to {entry.action} by a platform rule")
    if rule.is_wildcard:
        if entry.action != "deny":
            return ReportLine(element, "unsupported", "no flag; Claude's own default decides")
        if allowed:
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
    if not rule.native:
        return ReportLine(element, "unsupported", "no Claude tool name for this rule")
    flag = "--disallowedTools" if entry.action == "deny" else "--allowedTools"
    note = f"{flag} {','.join(rule.native)}"
    if entry.action == "ask":
        return ReportLine(
            element, "partial", f"{note}; Claude has no ask flag, so ask is sent as allow"
        )
    if entry.action == "allow":
        return ReportLine(element, "native", f"{note} (skips the prompt only)")
    return ReportLine(element, "native", note)


def _platform_lines(policy: PermissionPolicy) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for platform in policy.platform_rules:
        if platform.kind != "native-subagents":
            continue
        names = tuple(
            name
            for capability in platform.capabilities
            for name in _PLATFORM_DENY_TOOLS[capability]
        )
        lines.append(
            ReportLine(
                platform_element(platform), "platform", "--disallowedTools " + ",".join(names)
            )
        )
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
    effective: tuple[_Effective, ...],
) -> EnforcementReport:
    allowed = frozenset(permissions.allowed_tools)
    lines = [
        _tool_rule_line(entry, allowed)
        for entry in effective
        if entry.rule.native_status == "known"
    ]
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
    effective = _effective_rules(policy)
    allowed, disallowed = _compile_tool_rules(policy, effective)
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
    return permissions, _claude_report(policy, permissions, effective)


__all__ = [
    "ClaudePermissions",
    "compile_claude_permissions",
    "is_claude_agent_tool",
]
