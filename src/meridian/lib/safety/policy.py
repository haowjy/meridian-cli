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

from meridian.lib.tools import LEGACY_SNAPSHOT_ORIGIN, ToolRule, unscoped_action

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


type PlatformRuleKind = Literal["native-subagents", "builtin-agents", "completion-commands"]


class PlatformRule(BaseModel):
    """A rule Meridian puts on a launch whatever the profile says.

    ``native-subagents`` denies the harness's own sub-agent tools so delegation goes
    through Meridian; ``capabilities`` lists the ones to deny (``agent``, ``task``), already
    net of profile opt-outs. ``builtin-agents`` denies the harness's built-in agents.
    ``completion-commands`` is the one allow: ``commands`` are the shell commands a child
    runs to confirm or reject its own completion (``meridian spawn done`` / ``rearm``), which
    must stay runnable under any allowlist or deny of the shell. A harness spells each command
    and its argument form in its own scope syntax.
    A harness that has no native name for a rule leaves it out of its projection.
    """

    model_config = ConfigDict(frozen=True)

    kind: PlatformRuleKind
    capabilities: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


class PermissionPolicy(BaseModel):
    """Everything permission-shaped for one launch, before any harness reads it.

    Two kinds of allowlist, and the queries below are how a compile asks about them:

    - ``*: deny`` (the list form of ``tools`` writes one; a map may too): the policy is an
      allowlist, and only the tools it allows exist.
    - A capability whose rules are scoped allows and nothing unscoped (``bash(rg *): allow``
      and no ``bash`` rule): only those scopes may run, whatever ``approval`` says. A ``*``
      rule decides every unmatched call itself, so it ends the second kind.
    """

    model_config = ConfigDict(frozen=True)

    rules: tuple[ToolRule, ...] = ()
    platform_rules: tuple[PlatformRule, ...] = ()
    sandbox: SandboxMode = "default"
    approval: ApprovalMode = "default"

    @property
    def is_allowlist(self) -> bool:
        """Whether a ``*: deny`` rule makes every tool the rules do not allow unavailable."""

        return unscoped_action(self.rules, "*") == "deny"

    @property
    def allowlisted_capabilities(self) -> tuple[str, ...]:
        """Capabilities where only the scoped allows may run, in policy order."""

        if unscoped_action(self.rules, "*") is not None:
            return ()
        scoped_allows = dict.fromkeys(
            rule.capability
            for rule in self.rules
            if rule.capability is not None and rule.pattern is not None and rule.action == "allow"
        )
        return tuple(
            capability
            for capability in scoped_allows
            if unscoped_action(self.rules, capability) is None
        )

    @property
    def allowed_natives(self) -> tuple[str, ...]:
        """The harness's names for every rule that leaves a tool callable (``allow``, ``ask``).

        Policy order, deduplicated, exactly as Mars named them (scoped rules arrive scoped).
        Under ``is_allowlist`` these are the tools that remain; ``ask`` stays on the list
        because the harness, not the rule, decides that call.
        """

        return tuple(
            dict.fromkeys(
                name for rule in self.rules if rule.action != "deny" for name in rule.native
            )
        )


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


def allowlist_element(capability: str) -> str:
    """The report element for a capability whose scoped allows are its only permitted calls."""

    return f"tools: {capability} allowlist (only the scoped allows may run)"


def platform_element(rule: PlatformRule) -> str:
    if rule.kind == "native-subagents":
        return f"platform: deny native sub-agents ({', '.join(sorted(rule.capabilities))})"
    if rule.kind == "completion-commands":
        return f"platform: allow completion commands ({', '.join(rule.commands)})"
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

    Elements are the rules, the allowlisted capabilities, the platform rules, the sandbox and
    the approval mode.

    A compile reports only what it enforces or has something specific to say about; any
    element it left out gets the default line: a rule Mars could not name for ``harness`` is
    a gap, a deny of a tool ``harness`` lacks is satisfied, and anything else is
    ``unsupported``. Lines for things that are not policy elements (a deny the harness
    applies on its own) follow the elements.
    """

    defaults = [_rule_default_line(rule, harness) for rule in policy.rules]
    defaults.extend(
        ReportLine(allowlist_element(capability), "unsupported", f"not projected for {harness}")
        for capability in policy.allowlisted_capabilities
    )
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
    "allowlist_element",
    "build_permission_policy",
    "complete_report",
    "format_report_lines",
    "platform_element",
    "rule_element",
]
