"""Permission policy carried on the resolved launch spec.

One transport-neutral value built at bind: the tool rules, the platform rules Meridian
adds, the sandbox mode and the approval mode. Each harness compiles it into its own native
projection in ``lib/harness/projections/permissions_<harness>.py``, which also names the
platform rules natively; nothing here knows a harness.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.tools import LEGACY_SNAPSHOT_ORIGIN, ToolRule

type SandboxMode = Literal[
    "default",
    "read-only",
    "workspace-write",
    "danger-full-access",
]

type ApprovalMode = Literal[
    "default",
    "auto",
    "never",
    "yolo",
    "confirm",
]

_SANDBOX_MODES: frozenset[SandboxMode] = frozenset(
    {"default", "read-only", "workspace-write", "danger-full-access"}
)
_APPROVAL_MODES: frozenset[ApprovalMode] = frozenset(
    {"default", "auto", "never", "yolo", "confirm"}
)


type PlatformRuleKind = Literal["native-subagents", "builtin-agents"]


class PlatformRule(BaseModel):
    """A restriction Meridian puts on a launch whatever the profile says.

    ``native-subagents`` denies the harness's own sub-agent tools so delegation goes
    through Meridian; ``capabilities`` lists the ones to deny (``agent``, ``task``), already
    net of profile opt-outs. ``builtin-agents`` denies the harness's built-in agents.
    A harness that has no native name for a rule leaves it out of its projection.
    """

    model_config = ConfigDict(frozen=True)

    kind: PlatformRuleKind
    capabilities: tuple[str, ...] = ()


class PermissionPolicy(BaseModel):
    """Everything permission-shaped for one launch, before any harness reads it."""

    model_config = ConfigDict(frozen=True)

    rules: tuple[ToolRule, ...] = ()
    platform_rules: tuple[PlatformRule, ...] = ()
    sandbox: SandboxMode = "default"
    approval: ApprovalMode = "default"


type ReportStatus = Literal["native", "partial", "unsupported", "platform"]


@dataclass(frozen=True)
class ReportLine:
    """What one harness does with one element of the policy.

    ``native``: the harness enforces it as stated. ``partial``: part of its meaning reaches
    the harness; ``note`` says which part. ``unsupported``: nothing reaches the harness.
    ``platform``: a Meridian platform rule decides it, not the profile.
    """

    element: str
    status: ReportStatus
    note: str


type EnforcementReport = tuple[ReportLine, ...]


def rule_element(rule: ToolRule) -> str:
    """Name a rule the way the profile wrote it; a legacy snapshot rule has only its native key."""

    name = rule.capability if rule.capability is not None else ", ".join(rule.native)
    if rule.pattern is not None:
        name = f"{name}({rule.pattern})"
    legacy = " [legacy snapshot]" if rule.origin == LEGACY_SNAPSHOT_ORIGIN else ""
    return f"tools: {name} = {rule.action}{legacy}"


def platform_element(rule: PlatformRule) -> str:
    if rule.kind == "native-subagents":
        return f"platform: deny native sub-agents ({', '.join(sorted(rule.capabilities))})"
    return "platform: deny built-in agents"


def _rule_default_line(rule: ToolRule, harness: str) -> ReportLine:
    """What the report says about a rule the compile left unreported."""

    element = rule_element(rule)
    if rule.native_status == "unknown":
        return ReportLine(
            element,
            "unsupported",
            f"Mars has no {harness} name for this tool, so it is not enforced",
        )
    if rule.native_status == "absent":
        if rule.action == "deny":
            return ReportLine(element, "native", f"{harness} has no such tool, so nothing to deny")
        return ReportLine(element, "unsupported", f"{harness} lacks this tool")
    return ReportLine(element, "unsupported", f"not projected for {harness}")


def complete_report(
    policy: PermissionPolicy, reported: Iterable[ReportLine], *, harness: str
) -> EnforcementReport:
    """Give every policy element exactly one line, in policy order.

    A compile reports only what it enforces or has something specific to say about; any
    element it left out gets the default line: a rule Mars could not name for ``harness`` is
    a gap, a deny of a tool ``harness`` lacks is satisfied, and anything else is
    ``unsupported``. Lines for things that are not policy elements (a deny the harness
    applies on its own) follow the elements.
    """

    defaults = [_rule_default_line(rule, harness) for rule in policy.rules]
    defaults.extend(
        ReportLine(platform_element(rule), "unsupported", f"not projected for {harness}")
        for rule in policy.platform_rules
    )
    if policy.sandbox != "default":
        defaults.append(
            ReportLine(f"sandbox: {policy.sandbox}", "unsupported", f"not projected for {harness}")
        )
    if policy.approval != "default":
        defaults.append(
            ReportLine(
                f"approval: {policy.approval}", "unsupported", f"not projected for {harness}"
            )
        )
    by_element = {line.element: line for line in reported}
    lines = [by_element.get(default.element, default) for default in defaults]
    known = {default.element for default in defaults}
    lines.extend(line for line in by_element.values() if line.element not in known)
    return tuple(lines)


def format_report_lines(report: EnforcementReport) -> list[str]:
    """Render a report as indented text lines for dry-run output."""

    return [f"  [{line.status}] {line.element} -> {line.note}" for line in report]


def _parse_approval_value(raw: str) -> ApprovalMode:
    normalized = raw.strip().lower()
    if normalized in _APPROVAL_MODES:
        return normalized
    allowed = ", ".join(sorted(_APPROVAL_MODES))
    raise ValueError(f"Unsupported approval mode '{raw}'. Expected: {allowed}.")


def _parse_sandbox_value(raw: str | None) -> SandboxMode:
    normalized = raw.strip().lower() if raw else "default"
    if normalized in _SANDBOX_MODES:
        return normalized
    allowed = ", ".join(sorted(_SANDBOX_MODES))
    raise ValueError(f"Unsupported sandbox mode '{raw}'. Expected: {allowed}.")


def build_permission_policy(
    *,
    rules: tuple[ToolRule, ...] = (),
    sandbox: str | None,
    approval: str = "default",
    platform_rules: tuple[PlatformRule, ...] = (),
) -> PermissionPolicy:
    """Build the policy from Mars's resolved tool rules and the execution policy.

    ``sandbox`` and ``approval`` are the already-resolved execution policy values; this
    validates them and never re-resolves precedence.
    """

    return PermissionPolicy(
        rules=rules,
        platform_rules=platform_rules,
        sandbox=_parse_sandbox_value(sandbox),
        approval=_parse_approval_value(approval),
    )


__all__ = [
    "ApprovalMode",
    "EnforcementReport",
    "PermissionPolicy",
    "PlatformRule",
    "ReportLine",
    "ReportStatus",
    "SandboxMode",
    "build_permission_policy",
    "complete_report",
    "format_report_lines",
    "platform_element",
    "rule_element",
]
