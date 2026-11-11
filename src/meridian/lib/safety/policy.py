"""Permission policy carried on the resolved launch spec.

One transport-neutral value built at bind: the tool rules, the platform rules Meridian
adds, the sandbox mode and the approval mode. Each harness compiles it into its own native
projection in ``lib/harness/projections/permissions_<harness>.py``, which also names the
platform rules natively; nothing here knows a harness.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Generic, Literal, NewType, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from meridian.lib.tools import (
    LEGACY_SNAPSHOT_ORIGIN,
    PLATFORM_ORIGIN,
    PROJECT_ORIGIN,
    ToolRule,
    split_native,
    unscoped_action,
)

type SandboxMode = Literal[
    "default",
    "read-only",
    "workspace-write",
    "danger-full-access",
]
Host = NewType("Host", str)


@dataclass(frozen=True)
class Allow:
    """Unbounded network access, except for exact denied hosts."""

    deny: frozenset[Host] = frozenset()


@dataclass(frozen=True)
class Hosts:
    """Access limited to exact allowed hosts; denies always win."""

    allow: frozenset[Host] = frozenset()
    deny: frozenset[Host] = frozenset()


@dataclass(frozen=True)
class SandboxDerivedNetwork:
    """Host sets whose mode follows the final resolved sandbox."""

    allow: frozenset[Host] = frozenset()
    deny: frozenset[Host] = frozenset()


type NetworkPolicy = Allow | Hosts

# Shell network for a harness that does not confine its shell.
UNENFORCED_NETWORK: NetworkPolicy = Allow()


@dataclass(frozen=True)
class EffectivePolicy:
    """The sandbox and shell network a harness actually applies to one launch."""

    sandbox: SandboxMode
    network: NetworkPolicy


# What a harness that enforces neither sandbox nor network gives every launch.
UNENFORCED_POLICY = EffectivePolicy(sandbox="danger-full-access", network=UNENFORCED_NETWORK)
NativeNetworkT = TypeVar("NativeNetworkT", covariant=True)


@dataclass(frozen=True)
class NetworkProjection(Generic[NativeNetworkT]):
    """One harness's native network settings and the access they actually leave."""

    native: NativeNetworkT
    effective: NetworkPolicy


type ApprovalMode = Literal[
    "default",
    "auto",
    "never",
    "yolo",
    "confirm",
]

SANDBOX_MODE_WEAKNESS: dict[SandboxMode, int] = {
    "read-only": 0,
    "workspace-write": 1,
    "danger-full-access": 2,
    # A harness default is unknown, so compare it as weaker than every explicit mode.
    "default": 3,
}
_SANDBOX_MODES: frozenset[SandboxMode] = frozenset(
    {"default", "read-only", "workspace-write", "danger-full-access"}
)
CONSTRAINED_SANDBOX_MODES: frozenset[SandboxMode] = frozenset({"read-only", "workspace-write"})
_APPROVAL_MODES: frozenset[ApprovalMode] = frozenset(
    {"default", "auto", "never", "yolo", "confirm"}
)
_LOCAL_SERVICE_HOSTS = frozenset((Host("localhost"), Host("127.0.0.1")))


type PlatformRuleKind = Literal["builtin-agents", "meridian-commands"]
type CommandMatch = Literal["exact", "prefix"]


class PlatformCommand(BaseModel):
    """One Meridian-owned shell command grant."""

    model_config = ConfigDict(frozen=True)

    command: str
    match: CommandMatch
    runs_outside_sandbox: bool = False


class PlatformRule(BaseModel):
    """A rule Meridian puts on a launch whatever the profile says.

    Ordinary agent/task denies are ``ToolRule`` values named by Mars. ``builtin-agents``
    controls harness mechanisms that cannot be expressed as such a rule. The
    ``meridian-commands`` carries the shell commands Meridian itself makes available under
    any profile rule: completion signals, read operations, and child launch commands when
    the profile can spawn. A harness that decides them itself (Pi, Codex, OpenCode) allows
    them before it reads a rule; Claude checks its denies and asks first. A harness spells exact and
    prefix matches in its own syntax.
    """

    model_config = ConfigDict(frozen=True)

    kind: PlatformRuleKind
    commands: tuple[PlatformCommand, ...] = ()


class PermissionPolicy(BaseModel):
    """Everything permission-shaped for one launch, before any harness reads it.

    ``*: deny`` (a map writes it) makes the policy an allowlist: only the tools it allows
    exist. ``is_allowlist`` asks about it. A scoped allow (``bash(rg *): allow``) only
    pre-approves the calls it matches; it restricts nothing.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    rules: tuple[ToolRule, ...] = ()
    platform_rules: tuple[PlatformRule, ...] = ()
    sandbox: SandboxMode = "default"
    network: NetworkPolicy = Allow()
    # Workspace roots whose writes a read-only native sandbox must block. Bind keeps
    # these separate from projected runtime/temp roots, which must remain writable.
    sandbox_write_roots: tuple[str, ...] = Field(default=(), exclude=True)
    approval: ApprovalMode = "default"

    @model_validator(mode="before")
    @classmethod
    def _resolve_network_default(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        payload = dict(cast("Mapping[str, object]", value))
        if payload.get("network") is None:
            sandbox = _parse_sandbox_value(cast("str | None", payload.get("sandbox")))
            payload["network"] = resolve_network_policy(None, sandbox)
        return payload

    @field_validator("network", mode="before")
    @classmethod
    def _parse_network(cls, value: object) -> NetworkPolicy:
        parsed = parse_network_policy(value)
        if parsed is None:
            raise ValueError("network must be resolved on PermissionPolicy")
        return parsed

    @field_serializer("network")
    def _serialize_network(self, value: NetworkPolicy) -> dict[str, object]:
        return network_policy_to_wire(value)

    @property
    def outside_sandbox_commands(self) -> tuple[PlatformCommand, ...]:
        """Platform commands whose harness projection escapes native confinement."""

        return tuple(
            command
            for rule in self.platform_rules
            for command in rule.commands
            if command.runs_outside_sandbox
        )

    @property
    def has_spawn_capability(self) -> bool:
        """Whether this policy carries the canonical child-launch command grant."""

        return any(
            command.command == "meridian spawn"
            and command.match == "prefix"
            and command.runs_outside_sandbox
            for rule in self.platform_rules
            if rule.kind == "meridian-commands"
            for command in rule.commands
        )

    @property
    def is_allowlist(self) -> bool:
        """Whether a ``*: deny`` rule makes every tool the rules do not allow unavailable."""

        return unscoped_action(self.rules, "*") == "deny"


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

    def to_wire(self) -> dict[str, str]:
        """Return the stable public permission-report shape."""

        return {"element": self.element, "status": self.status, "note": self.note}


type EnforcementReport = tuple[ReportLine, ...]


def rule_element(rule: ToolRule) -> str:
    """Name a rule the way the profile wrote it; a legacy snapshot rule has only its native key."""

    if rule.origin == PLATFORM_ORIGIN:
        return f"platform: tools {rule.capability} = {rule.action}"
    name = rule.capability if rule.capability is not None else ", ".join(rule.native)
    if rule.pattern is not None:
        name = f"{name}({rule.pattern})"
    if rule.origin == PROJECT_ORIGIN:
        return f"project: tools {name} = {rule.action}"
    legacy = " [legacy snapshot]" if rule.origin == LEGACY_SNAPSHOT_ORIGIN else ""
    return f"tools: {name} = {rule.action}{legacy}"


def platform_element(rule: PlatformRule) -> str:
    if rule.kind == "meridian-commands":
        commands = ", ".join(f"{command.match}: {command.command}" for command in rule.commands)
        return f"platform: allow Meridian commands ({commands})"
    return "platform: deny built-in agents"


def _rule_default_line(rule: ToolRule, harness: str) -> ReportLine:
    """What the report says about a rule the compile left unreported."""

    element = rule_element(rule)
    satisfied_status: ReportStatus = "platform" if rule.origin == PLATFORM_ORIGIN else "native"
    if rule.covered_by is not None and rule.native_status == "known":
        covering = f"{rule.covered_by.action} {rule.covered_by.key} covers it"
        return ReportLine(element, satisfied_status, rule.note or covering)
    if rule.native_status == "unknown":
        return ReportLine(
            element,
            "unsupported",
            (
                rule.note
                if rule.origin == PLATFORM_ORIGIN and rule.note
                else f"Mars has no {harness} name for this tool, so it is not enforced"
            ),
        )
    if rule.native_status == "absent":
        if rule.action == "deny":
            return ReportLine(
                element, satisfied_status, f"{harness} has no such tool, so nothing to deny"
            )
        return ReportLine(element, "unsupported", f"{harness} lacks this tool")
    return ReportLine(element, "unsupported", f"not projected for {harness}")


def command_in_scope(command: PlatformCommand, pattern: str | None) -> bool:
    """Whether a shell rule's ``pattern`` (``None``: every command) reaches a command grant.

    Reaches means the rule matches the grant's command, or lies wholly inside it.
    """

    if pattern is None or pattern == command.command:
        return True
    if command.match == "prefix" and pattern.startswith(f"{command.command} "):
        return True
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), command.command) is not None


def _overridden_grants(rule: ToolRule, platforms: Iterable[PlatformRule]) -> list[str]:
    """The Meridian command grants a deny or ask of ``rule`` does not stop."""

    if (
        rule.origin == PLATFORM_ORIGIN
        or rule.action == "allow"
        or rule.capability
        not in {
            "bash",
            "*",
        }
    ):
        return []
    return [
        command.command
        for platform in platforms
        if platform.kind == "meridian-commands"
        for command in platform.commands
        if command_in_scope(command, rule.pattern)
    ]


def _spilled_permissions(rules: Iterable[ToolRule]) -> dict[str, str]:
    """Unscoped rules a scoped rule's permission spelling reaches past its own tools.

    Mars may spell a scoped rule with another tool's permission (Claude scopes Write through
    ``Edit(path)``) and notes it. That permission also governs the other tool at that scope,
    so an unscoped rule of the opposite action on that tool no longer holds there.
    """

    rules = tuple(rules)
    spilled: dict[str, str] = {}
    for scoped in rules:
        if not scoped.note or not scoped.is_scoped:
            continue
        for native in scoped.native:
            base = split_native(native)[0]
            if base in scoped.tools:
                continue
            for plain in rules:
                if plain.is_scoped or plain.action == scoped.action or base not in plain.tools:
                    continue
                spilled.setdefault(
                    rule_element(plain),
                    f"{rule_element(scoped)} is spelled {native}, which also governs {base} there",
                )
    return spilled


def complete_report(
    policy: PermissionPolicy,
    reported: Iterable[ReportLine],
    *,
    harness: str,
    grants_first: bool = True,
) -> EnforcementReport:
    """Give every policy element exactly one line, in policy order.

    Elements are the rules, the platform rules, the sandbox, the network policy, and the
    approval mode.

    A compile reports only what it enforces or has something specific to say about; any
    element it left out gets the default line: a rule Mars could not name for ``harness`` is
    a gap, a deny of a tool ``harness`` lacks is satisfied, and anything else is
    ``unsupported``. A rule Mars attached a note to is at best ``partial``, with the note. A rule
    Mars covered (its ``covered_by`` names the stricter rule that decides every call it names) is
    reported as Mars noted it, whatever the compile said. A deny or ask of shell commands is
    ``partial`` when ``grants_first`` (the harness allows Meridian's command grants before it reads
    a rule) and a grant lies in its scope. Lines
    for things that are not policy elements (a deny the harness
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
    effective_network = Allow()
    defaults.append(
        ReportLine(
            network_policy_element(policy.network),
            "native" if effective_network == policy.network else "unsupported",
            f"{harness} leaves shell network unrestricted",
        )
    )
    if policy.approval != "default":
        defaults.append(
            ReportLine(
                f"approval: {policy.approval}", "unsupported", f"not projected for {harness}"
            )
        )
    by_element = {line.element: line for line in reported}
    rules_by_element = {rule_element(rule): rule for rule in policy.rules}
    spilled = _spilled_permissions(policy.rules)
    lines: list[ReportLine] = []
    for default in defaults:
        line = by_element.get(default.element, default)
        rule = rules_by_element.get(default.element)
        if rule is not None and rule.covered_by is not None and rule.native_status == "known":
            line = default
        elif rule is not None and rule.origin == PLATFORM_ORIGIN and line.status == "native":
            line = ReportLine(line.element, "platform", line.note)
        elif rule is not None and rule.note and line.status in ("native", "partial"):
            # Mars notes a rule only when its native spelling does not carry the rule exactly
            # (a shared permission name, an overlap, a shadowed scope), so it is not native.
            # A compile's own partial note says how it falls short, not what Mars saw.
            note = rule.note if not line.note else f"{line.note}; {rule.note}"
            line = ReportLine(line.element, "partial", note)
        if rule is not None and grants_first and line.status in ("native", "platform", "partial"):
            grants = _overridden_grants(rule, policy.platform_rules)
            if grants:
                shown = ", ".join(f"`{grant}`" for grant in grants[:3])
                more = f" and {len(grants) - 3} more" if len(grants) > 3 else ""
                line = ReportLine(
                    line.element,
                    "partial",
                    f"{line.note}; {harness} allows Meridian's own commands first, so {shown}"
                    f"{more} still run",
                )
        if line.element in spilled and line.status == "native":
            line = ReportLine(line.element, "partial", f"{line.note}; {spilled[line.element]}")
        lines.append(line)
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


def canonical_host(raw: str) -> Host:
    """Canonicalize one exact host token without interpreting wildcards."""

    normalized = raw.strip().lower().removesuffix(".")
    if not normalized:
        raise ValueError("network hosts must not be empty")
    return Host(normalized)


def host_set(values: Iterable[str]) -> frozenset[Host]:
    """Canonicalize and deduplicate exact host strings."""

    return frozenset(canonical_host(value) for value in values if value.strip())


def default_network_policy(sandbox: SandboxMode) -> NetworkPolicy:
    """Keep a projected sandbox offline unless the profile opts in."""

    return Hosts() if sandbox in CONSTRAINED_SANDBOX_MODES else Allow()


def parse_network_policy(raw: object) -> NetworkPolicy | None:
    """Parse the scalar or durable wire form at a trust boundary."""

    if raw is None or isinstance(raw, Allow | Hosts):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == "allow":
            return Allow()
        if normalized == "deny":
            return Hosts()
        raise ValueError(f"Unsupported network mode '{raw}'. Expected: allow, deny.")
    if not isinstance(raw, Mapping):
        raise ValueError("network must be allow, deny, or a typed network object")
    payload = cast("Mapping[object, object]", raw)
    raw_allow = payload.get("allow")
    raw_deny = payload.get("deny", ())
    if not isinstance(raw_deny, (list, tuple, frozenset, set)):
        raise ValueError("network.deny must be a list of hosts")
    deny_values = cast("Iterable[object]", raw_deny)
    if not all(isinstance(value, str) for value in deny_values):
        raise ValueError("network.deny must be a list of hosts")
    deny = host_set(cast("Iterable[str]", raw_deny))
    if raw_allow == "*":
        return Allow(deny=deny)
    if not isinstance(raw_allow, (list, tuple, frozenset, set)):
        raise ValueError("network.allow must be '*' or a list of hosts")
    allow_values = cast("Iterable[object]", raw_allow)
    if not all(isinstance(value, str) for value in allow_values):
        raise ValueError("network.allow must be '*' or a list of hosts")
    return Hosts(allow=host_set(cast("Iterable[str]", raw_allow)), deny=deny)


def network_policy_to_wire(policy: NetworkPolicy) -> dict[str, object]:
    """Serialize one policy deterministically for durable and environment records."""

    allowed: str | list[str] = "*" if isinstance(policy, Allow) else sorted(policy.allow)
    return {"allow": allowed, "deny": sorted(policy.deny)}


def resolve_network_policy(
    policy: NetworkPolicy | SandboxDerivedNetwork | None,
    sandbox: str | None,
) -> NetworkPolicy:
    """Resolve a sandbox-derived policy after sandbox precedence is final."""

    resolved_sandbox = _parse_sandbox_value(sandbox)
    if policy is None:
        return default_network_policy(resolved_sandbox)
    if isinstance(policy, SandboxDerivedNetwork):
        if resolved_sandbox in CONSTRAINED_SANDBOX_MODES:
            return Hosts(deny=policy.deny)
        return Allow(deny=policy.deny)
    return policy


def network_policy_name(policy: NetworkPolicy) -> Literal["allow", "deny", "allowlist"]:
    """Name a policy without expanding or interpreting host wildcards."""

    if isinstance(policy, Allow):
        return "allow"
    if not policy.allow:
        return "deny"
    return "allowlist"


def format_network_policy(policy: NetworkPolicy) -> str:
    """Return a stable count-plus-preview rendering for reports and refusals."""

    name = network_policy_name(policy)
    sets: list[str] = []
    if isinstance(policy, Hosts) and policy.allow:
        sets.append(_format_host_set("allow", policy.allow))
    if policy.deny:
        sets.append(_format_host_set("deny", policy.deny))
    return name if not sets else f"{name} ({'; '.join(sets)})"


def _format_host_set(label: str, hosts: frozenset[Host]) -> str:
    ordered = sorted(hosts)
    preview: list[str] = list(ordered[:3])
    if len(ordered) > len(preview):
        preview.append("…")
    return f"{label}={len(ordered)} [{', '.join(preview)}]"


def network_policy_element(policy: NetworkPolicy) -> str:
    """Return the canonical report element used to join projector results."""

    return f"network: {format_network_policy(policy)}"


def network_policy_contains(caller: NetworkPolicy, child: NetworkPolicy) -> bool:
    """Return whether the child's exact host policy is contained by the caller's."""

    if isinstance(child, Hosts) and not child.allow - child.deny:
        return True
    if not child.deny.issuperset(caller.deny):
        return False
    if isinstance(caller, Allow):
        return True
    return isinstance(child, Hosts) and child.allow.issubset(caller.allow)


def network_allows_local_services(policy: NetworkPolicy) -> bool:
    """Return whether the policy opts into Claude's coarse local-service switches."""

    if isinstance(policy, Allow):
        return bool(_LOCAL_SERVICE_HOSTS - policy.deny)
    return bool((policy.allow - policy.deny) & _LOCAL_SERVICE_HOSTS)


def build_permission_policy(
    *,
    rules: tuple[ToolRule, ...] = (),
    sandbox: str | None,
    network: NetworkPolicy,
    approval: str = "default",
    platform_rules: tuple[PlatformRule, ...] = (),
    sandbox_write_roots: tuple[str, ...] = (),
) -> PermissionPolicy:
    """Build the policy from Mars's resolved tool rules and the execution policy.

    ``network`` and ``approval`` are already-resolved execution policy values; this never
    re-resolves precedence.
    """

    return PermissionPolicy(
        rules=rules,
        platform_rules=platform_rules,
        sandbox=_parse_sandbox_value(sandbox),
        network=network,
        sandbox_write_roots=tuple(dict.fromkeys(root for root in sandbox_write_roots if root)),
        approval=_parse_approval_value(approval),
    )


__all__ = [
    "CONSTRAINED_SANDBOX_MODES",
    "SANDBOX_MODE_WEAKNESS",
    "UNENFORCED_NETWORK",
    "UNENFORCED_POLICY",
    "Allow",
    "ApprovalMode",
    "CommandMatch",
    "EffectivePolicy",
    "EnforcementReport",
    "Host",
    "Hosts",
    "NetworkPolicy",
    "NetworkProjection",
    "PermissionPolicy",
    "PlatformCommand",
    "PlatformRule",
    "ReportLine",
    "ReportStatus",
    "SandboxDerivedNetwork",
    "SandboxMode",
    "build_permission_policy",
    "canonical_host",
    "complete_report",
    "default_network_policy",
    "format_network_policy",
    "format_report_lines",
    "host_set",
    "network_allows_local_services",
    "network_policy_contains",
    "network_policy_element",
    "network_policy_name",
    "network_policy_to_wire",
    "parse_network_policy",
    "platform_element",
    "resolve_network_policy",
    "rule_element",
]
