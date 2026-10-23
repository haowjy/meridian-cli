"""Compile permission policy into Pi tool flags and extension rules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.native_config import unsupported_native_config_report
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.safety.policy import (
    ALLOWLIST_ORIGIN,
    EnforcementReport,
    PermissionPolicy,
    PlatformCommand,
    ReportLine,
    command_grant_covers_pattern,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule, split_native

PI_PERMISSION_POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY"
# Pi's tool-selection flags and their short forms; under a bound policy the compile owns them.
_TOOL_SELECTION_FLAGS: tuple[tuple[str, ...], ...] = (
    ("--tools", "-t"),
    ("--exclude-tools", "-xt"),
    ("--no-tools", "-nt"),
    ("--no-builtin-tools", "-nbt"),
)
_STRICTNESS: dict[ToolAction, int] = {"allow": 0, "ask": 1, "deny": 2}


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
    meridian_commands: tuple[PlatformCommand, ...] = ()
    headless_reject_reason: str = HEADLESS_REJECT_REASON
    # Whether the handler refuses a bash command it cannot split into simple commands.
    bash_fail_closed: bool = False


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
                    # The model reads the label in the refusal; an allowlist's deny says why.
                    label=(
                        f"{tool} outside its scoped allows"
                        if rule.origin == ALLOWLIST_ORIGIN
                        else native
                    ),
                )
            )
    return tuple(projected)


def _meridian_commands(policy: PermissionPolicy) -> tuple[PlatformCommand, ...]:
    return tuple(
        command
        for platform in policy.platform_rules
        if platform.kind == "meridian-commands"
        for command in platform.commands
    )


def _strictest(rules: Iterable[PiExtensionRule]) -> ToolAction | None:
    """The strictest action among ``rules``, as the extension ranks equally specific rules."""

    return max((rule.action for rule in rules), key=_STRICTNESS.__getitem__, default=None)


def _compile_flags(
    policy: PermissionPolicy,
    rules: tuple[PiExtensionRule, ...],
    meridian_commands: tuple[PlatformCommand, ...],
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    named = tuple(
        rule for rule in policy.rules if rule.native_status == "known" and not rule.is_wildcard
    )
    # A plain deny removes its tools unless a scoped rule still needs them; the extension
    # then refuses what the scoped rules don't allow.
    scoped_tools = frozenset(tool for rule in named if rule.is_scoped for tool in rule.tools)
    meridian_tools = frozenset({"bash"} if meridian_commands else ())

    exclude = tuple(
        dedupe_nonempty(
            tool
            for rule in named
            if rule.action == "deny" and not rule.is_scoped
            for tool in rule.tools
            if tool not in scoped_tools and tool not in meridian_tools
        )
    )
    callable_tools = policy.allowed_tools
    wildcard = _strictest(rule for rule in rules if rule.tool == "*" and rule.pattern is None)
    if wildcard == "deny":
        allowed = tuple(dedupe_nonempty((*callable_tools, *meridian_tools)))
        return allowed, exclude
    if wildcard in {"allow", "ask"}:
        return ("*",), exclude
    additions = tuple(f"+{tool}" for tool in callable_tools)
    return (additions or None), exclude


def _bash_fails_closed(rules: tuple[PiExtensionRule, ...]) -> bool:
    """Whether a bash command the handler cannot split must be refused.

    It must when such a command could be refused or asked for without matching a scoped
    rule: the rule deciding unmatched commands (bash's own unscoped rule, else ``*``'s) is
    ``deny`` or ``ask``, as in every allowlist, or a scoped ``ask`` could match it. Under an
    unscoped allow, or no rule, only a scoped deny refuses, so that check is best effort: a
    command the splitter cannot read runs.
    """

    shell = tuple(rule for rule in rules if rule.tool in {"bash", "*"})
    if any(rule.pattern is not None and rule.action == "ask" for rule in shell):
        return True
    for tool in ("bash", "*"):
        unmatched = _strictest(rule for rule in shell if rule.tool == tool and rule.pattern is None)
        if unmatched is not None:
            return unmatched != "allow"
    return False


def _needs_extension(
    rules: tuple[PiExtensionRule, ...], *, meridian_commands: tuple[PlatformCommand, ...]
) -> bool:
    # Meridian commands need the extension only when a rule could otherwise restrict the shell.
    return bool(
        (meridian_commands and rules)
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
    names = rule.tools
    if rule.pattern is not None or rule.action == "ask":
        note = "tool_call handler for " + ",".join(rule.native)
        policy = permissions.extension_policy
        if (
            rule.action == "deny"
            and "bash" in names
            and policy is not None
            and not policy.bash_fail_closed
        ):
            return ReportLine(
                rule_element(rule),
                "partial",
                f"{note}; best effort, since bash runs unmatched commands: a command the "
                "handler cannot split into simple commands (expansion, redirection, loops, "
                "here-docs, a lone &) runs unchecked",
            )
        if (
            rule.pattern is not None
            and "bash" in names
            and policy is not None
            and policy.bash_fail_closed
        ):
            note += (
                "; bash fails closed: any command the handler cannot split into simple "
                "commands (expansion, redirection, loops, here-docs, a lone &) is refused, "
                "even one no rule names"
            )
        return ReportLine(rule_element(rule), "native", note)
    if rule.action == "deny" and all(name in permissions.exclude_tools for name in names):
        return ReportLine(rule_element(rule), "native", "--exclude-tools " + ",".join(names))
    if rule.action == "allow" and permissions.tools is not None:
        return ReportLine(rule_element(rule), "native", "--tools " + ",".join(permissions.tools))
    if rule.action == "deny":
        return ReportLine(
            rule_element(rule), "native", "tool_call handler refuses calls no scoped rule allows"
        )
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
    for platform in policy.platform_rules:
        if platform.kind == "meridian-commands":
            stricter = any(
                rule.tool in {"bash", "*"}
                and rule.pattern is not None
                and rule.action != "allow"
                and any(
                    command_grant_covers_pattern(command, rule.pattern)
                    for command in platform.commands
                )
                for rule in extension_rules
            )
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "partial" if stricter else "platform",
                    (
                        "--tools bash plus fixed tool_call grants; narrower profile rules "
                        "remain stricter"
                        if stricter
                        else "--tools bash plus fixed tool_call grants"
                    )
                    if permissions.extension_policy is not None
                    else "no rule restricts bash, so they run",
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
    lines.extend(unsupported_native_config_report(spec.native_config, harness="Pi"))
    lines.extend(compaction_report(spec, harness="Pi"))
    return complete_report(policy, lines, harness="Pi")


def compile_pi_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[PiPermissions, EnforcementReport]:
    """Compile Pi flags, extension policy, and an honest enforcement report."""

    policy = spec.permission_policy
    rules = _extension_rules(policy.rules)
    meridian_commands = _meridian_commands(policy)
    tools, exclude_tools = _compile_flags(policy, rules, meridian_commands)
    extension_policy = (
        PiExtensionPolicy(
            rules=rules,
            meridian_commands=meridian_commands,
            bash_fail_closed=_bash_fails_closed(rules),
        )
        if _needs_extension(rules, meridian_commands=meridian_commands)
        else None
    )
    permissions = PiPermissions(
        tools=tools,
        exclude_tools=exclude_tools,
        extension_policy=extension_policy,
    )
    return permissions, _report(spec, permissions, rules)


def reject_passthrough_tool_flags(passthrough: tuple[str, ...], policy: PermissionPolicy) -> None:
    """Refuse passthrough tool selection on a launch whose profile has tool rules.

    The compile owns Pi's tool flags there; a second selection from passthrough could
    widen what the profile allows.
    """

    if not policy.rules:
        return
    for flag, *aliases in _TOOL_SELECTION_FLAGS:
        if any(
            token == alias or token.startswith(f"{alias}=")
            for alias in (flag, *aliases)
            for token in passthrough
        ):
            raise ValueError(
                "Pi launches with a profile cannot accept tool-selection flags from "
                f"passthrough extra_args; remove {flag} so the profile remains binding"
            )


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
    "reject_passthrough_tool_flags",
]
