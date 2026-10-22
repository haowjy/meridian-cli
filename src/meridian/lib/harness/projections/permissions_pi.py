"""Compile permission policy into Pi tool flags and extension rules."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
    allowlist_element,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule, split_native

PI_PERMISSION_POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY"


class PiExtensionRule(BaseModel):
    """One native Pi rule evaluated by the bundled permission extension."""

    model_config = ConfigDict(frozen=True)

    capability: str
    tool: str
    pattern: str | None = None
    action: ToolAction
    label: str


class PiExtensionPolicy(BaseModel):
    """Launch-local input for Pi's ``tool_call`` permission handler."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    rules: tuple[PiExtensionRule, ...]
    allowlist_tools: tuple[str, ...] = ()
    completion_commands: tuple[str, ...] = ()
    headless_reject_reason: str = HEADLESS_REJECT_REASON


@dataclass(frozen=True)
class PiPermissions:
    """Pi's launch-time permission projection.

    ``tools`` is ``None`` when no ``--tools`` flag is needed. An empty tuple means
    ``--no-tools``. Entries beginning with ``+`` add tools without replacing Pi's
    defaults; plain entries form an allowlist.
    """

    tools: tuple[str, ...] | None = None
    exclude_tools: tuple[str, ...] = ()
    extension_policy: PiExtensionPolicy | None = None


def _extension_rules(rules: tuple[ToolRule, ...]) -> tuple[PiExtensionRule, ...]:
    projected: list[PiExtensionRule] = []
    for rule in rules:
        if rule.is_wildcard:
            projected.append(
                PiExtensionRule(
                    capability="*",
                    tool="*",
                    action=rule.action,
                    label="*",
                )
            )
            continue
        if rule.native_status != "known":
            continue
        for native in rule.native:
            tool, pattern = split_native(native)
            projected.append(
                PiExtensionRule(
                    capability=rule.capability or tool,
                    tool=tool,
                    pattern=pattern,
                    action=rule.action,
                    label=native,
                )
            )
    return tuple(projected)


def _completion_commands(policy: PermissionPolicy, *, interactive: bool) -> tuple[str, ...]:
    if interactive:
        return ()
    return tuple(
        command
        for platform in policy.platform_rules
        if platform.kind == "completion-commands"
        for command in platform.commands
    )


def _allowlist_tools(
    policy: PermissionPolicy, rules: tuple[PiExtensionRule, ...]
) -> tuple[str, ...]:
    capabilities = frozenset(policy.allowlisted_capabilities)
    return tuple(dedupe_nonempty(rule.tool for rule in rules if rule.capability in capabilities))


def _compile_flags(
    policy: PermissionPolicy,
    rules: tuple[PiExtensionRule, ...],
    completion_commands: tuple[str, ...],
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    exact = tuple(rule for rule in rules if rule.tool != "*" and rule.pattern is None)
    scoped_tools = frozenset(rule.tool for rule in rules if rule.pattern is not None)
    completion_tools = frozenset({"bash"} if completion_commands else ())

    exclude = tuple(
        dedupe_nonempty(
            rule.tool
            for rule in exact
            if rule.action == "deny"
            and rule.tool not in scoped_tools
            and rule.tool not in completion_tools
        )
    )
    callable_tools = tuple(
        dedupe_nonempty(split_native(native)[0] for native in policy.allowed_natives)
    )
    wildcard = max(
        (rule.action for rule in rules if rule.tool == "*" and rule.pattern is None),
        key={"allow": 0, "ask": 1, "deny": 2}.__getitem__,
        default=None,
    )
    if wildcard == "deny":
        allowed = tuple(dedupe_nonempty((*callable_tools, *completion_tools)))
        return allowed, exclude
    if wildcard in {"allow", "ask"}:
        return ("*",), exclude
    additions = tuple(f"+{tool}" for tool in callable_tools)
    return (additions or None), exclude


def _needs_extension(
    rules: tuple[PiExtensionRule, ...],
    *,
    allowlist_tools: tuple[str, ...],
    completion_commands: tuple[str, ...],
) -> bool:
    # Every spawn carries the completion commands; they need the extension only when a
    # rule could otherwise restrict the shell.
    return bool(
        allowlist_tools
        or (completion_commands and rules)
        or any(
            rule.pattern is not None
            or rule.action == "ask"
            or (rule.tool == "*" and rule.action == "deny")
            for rule in rules
        )
    )


def _rule_report_line(
    rule: ToolRule,
    *,
    extension_rules: tuple[PiExtensionRule, ...],
    permissions: PiPermissions,
) -> ReportLine | None:
    projected = tuple(
        candidate
        for candidate in extension_rules
        if candidate.capability == (rule.capability or candidate.tool)
        and candidate.action == rule.action
        and candidate.pattern == rule.pattern
    )
    if rule.is_wildcard:
        if rule.action == "deny":
            flag = (
                "--no-tools"
                if permissions.tools == ()
                else f"--tools {','.join(permissions.tools or ())}"
            )
            return ReportLine(
                rule_element(rule), "native", f"{flag}; extension blocks surviving calls"
            )
        if rule.action == "ask":
            return ReportLine(
                rule_element(rule),
                "native",
                "tool_call handler asks in a primary and rejects in a spawn",
            )
        return ReportLine(rule_element(rule), "native", "--tools *")
    if not projected:
        return None
    names = tuple(dedupe_nonempty(candidate.tool for candidate in projected))
    if rule.pattern is not None or rule.action == "ask":
        return ReportLine(
            rule_element(rule), "native", "tool_call handler for " + ",".join(rule.native)
        )
    if rule.action == "deny" and all(name in permissions.exclude_tools for name in names):
        return ReportLine(rule_element(rule), "native", "--exclude-tools " + ",".join(names))
    if rule.action == "allow" and permissions.tools is not None:
        return ReportLine(rule_element(rule), "native", "--tools " + ",".join(permissions.tools))
    return ReportLine(rule_element(rule), "native", "tool_call handler preserves scoped precedence")


def _report(
    spec: ResolvedLaunchSpec,
    permissions: PiPermissions,
    extension_rules: tuple[PiExtensionRule, ...],
) -> EnforcementReport:
    policy = spec.permission_policy
    lines = [
        line
        for rule in policy.rules
        if rule.native_status == "known"
        if (
            line := _rule_report_line(
                rule,
                extension_rules=extension_rules,
                permissions=permissions,
            )
        )
        is not None
    ]
    if permissions.extension_policy is not None:
        lines.extend(
            ReportLine(
                allowlist_element(capability),
                "native",
                "tool_call handler blocks unmatched calls",
            )
            for capability in policy.allowlisted_capabilities
        )
    for platform in policy.platform_rules:
        if platform.kind == "completion-commands" and not spec.interactive:
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "--tools bash plus fixed tool_call grants"
                    if permissions.extension_policy is not None
                    else "no rule restricts bash, so they run",
                )
            )
        elif platform.kind == "native-subagents":
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "Pi has no native sub-agent tools",
                )
            )
        elif platform.kind == "builtin-agents":
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "Pi has no built-in agents",
                )
            )
    return complete_report(policy, lines, harness="Pi")


def compile_pi_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[PiPermissions, EnforcementReport]:
    """Compile Pi flags, extension policy, and an honest enforcement report."""

    policy = spec.permission_policy
    rules = _extension_rules(policy.rules)
    completion_commands = _completion_commands(policy, interactive=spec.interactive)
    allowlist_tools = _allowlist_tools(policy, rules)
    tools, exclude_tools = _compile_flags(policy, rules, completion_commands)
    extension_policy = (
        PiExtensionPolicy(
            rules=rules,
            allowlist_tools=allowlist_tools,
            completion_commands=completion_commands,
        )
        if _needs_extension(
            rules,
            allowlist_tools=allowlist_tools,
            completion_commands=completion_commands,
        )
        else None
    )
    permissions = PiPermissions(
        tools=tools,
        exclude_tools=exclude_tools,
        extension_policy=extension_policy,
    )
    return permissions, _report(spec, permissions, rules)


def project_pi_permission_args(permissions: PiPermissions) -> tuple[str, ...]:
    """Render the compiled Pi permission flags for either launch surface."""

    args: list[str] = []
    if permissions.tools == ():
        args.append("--no-tools")
    elif permissions.tools is not None:
        args.extend(("--tools", ",".join(permissions.tools)))
    if permissions.exclude_tools:
        args.extend(("--exclude-tools", ",".join(permissions.exclude_tools)))
    return tuple(args)


__all__ = [
    "PI_PERMISSION_POLICY_ENV",
    "PiExtensionPolicy",
    "PiExtensionRule",
    "PiPermissions",
    "compile_pi_permissions",
    "project_pi_permission_args",
]
