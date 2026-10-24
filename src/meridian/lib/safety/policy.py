"""Permission policy carried on the resolved launch spec.

One transport-neutral value built at bind: the tool rules, the platform rules Meridian
adds, the sandbox mode and the approval mode. Each harness compiles it into its own native
projection in ``lib/harness/projections/permissions_<harness>.py``, which also names the
platform rules natively; nothing here knows a harness.
"""

from __future__ import annotations

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


# The origin of the unscoped deny ``build_permission_policy`` writes for a per-capability
# allowlist; Mars never writes it.
ALLOWLIST_ORIGIN = "allowlist"


type PlatformRuleKind = Literal["builtin-agents", "meridian-commands"]
type CommandMatch = Literal["exact", "prefix"]
type PlatformCommandRole = Literal["completion", "read", "launch"]


class PlatformCommand(BaseModel):
    """One Meridian-owned shell command grant."""

    model_config = ConfigDict(frozen=True)

    command: str
    match: CommandMatch
    role: PlatformCommandRole = "read"
    runs_outside_sandbox: bool = False

    @property
    def profile_restrictable(self) -> bool:
        """Whether a narrower profile rule may keep this command restricted."""

        return self.role != "completion"


class PlatformRule(BaseModel):
    """A rule Meridian puts on a launch whatever the profile says.

    Ordinary agent/task denies are ``ToolRule`` values named by Mars. ``builtin-agents``
    controls harness mechanisms that cannot be expressed as such a rule. The
    ``meridian-commands`` carries the shell commands Meridian itself makes available under
    any profile allowlist: completion signals, read operations, and child launch commands
    when the profile can spawn. A harness spells exact and prefix matches in its own syntax.
    """

    model_config = ConfigDict(frozen=True)

    kind: PlatformRuleKind
    commands: tuple[PlatformCommand, ...] = ()


class PermissionPolicy(BaseModel):
    """Everything permission-shaped for one launch, before any harness reads it.

    Two kinds of allowlist:

    - ``*: deny`` (a map writes it): the policy is an
      allowlist, and only the tools it allows exist. ``is_allowlist`` asks about it.
    - A capability whose rules are scoped allows and nothing unscoped (``bash(rg *): allow``
      and no ``bash`` rule): only those scopes may run, whatever ``approval`` says. The
      builder writes it into ``rules`` as the unscoped deny it means (origin
      ``ALLOWLIST_ORIGIN``), so a compile handles it as any plain deny beside scoped allows.
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
            command.role == "launch"
            and command.command == "meridian spawn"
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

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        """The harness tools some rule leaves callable (``allow``, ``ask``), unscoped.

        Policy order, deduplicated, from each rule's ``tools``. Under ``is_allowlist`` these
        are the tools that remain; ``ask`` stays on the list because the harness, not the
        rule, decides that call.
        """

        return tuple(
            dict.fromkeys(
                tool for rule in self.rules if rule.action != "deny" for tool in rule.tools
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

    def to_wire(self) -> dict[str, str]:
        """Return the stable public permission-report shape."""

        return {"element": self.element, "status": self.status, "note": self.note}


type EnforcementReport = tuple[ReportLine, ...]


def rule_element(rule: ToolRule) -> str:
    """Name a rule the way the profile wrote it; a legacy snapshot rule has only its native key."""

    if rule.origin == PLATFORM_ORIGIN:
        return f"platform: tools {rule.capability} = {rule.action}"
    if rule.origin == ALLOWLIST_ORIGIN:
        return f"tools: {rule.capability} allowlist (only the scoped allows may run)"
    name = rule.capability if rule.capability is not None else ", ".join(rule.native)
    if rule.pattern is not None:
        name = f"{name}({rule.pattern})"
    legacy = " [legacy snapshot]" if rule.origin == LEGACY_SNAPSHOT_ORIGIN else ""
    return f"tools: {name} = {rule.action}{legacy}"


def platform_element(rule: PlatformRule) -> str:
    if rule.kind == "meridian-commands":
        commands = ", ".join(f"{command.match}: {command.command}" for command in rule.commands)
        return f"platform: allow Meridian commands ({commands})"
    return "platform: deny built-in agents"


def command_grant_covers_pattern(command: PlatformCommand, pattern: str) -> bool:
    """Whether a scoped shell pattern is wholly inside a platform command grant."""

    if command.match == "exact":
        return pattern == command.command
    return pattern == command.command or pattern.startswith(f"{command.command} ")


def _rule_default_line(rule: ToolRule, harness: str) -> ReportLine:
    """What the report says about a rule the compile left unreported."""

    element = rule_element(rule)
    satisfied_status: ReportStatus = "platform" if rule.origin == PLATFORM_ORIGIN else "native"
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
) -> EnforcementReport:
    """Give every policy element exactly one line, in policy order.

    Elements are the rules, the platform rules, the sandbox, the network policy, and the
    approval mode.

    A compile reports only what it enforces or has something specific to say about; any
    element it left out gets the default line: a rule Mars could not name for ``harness`` is
    a gap, a deny of a tool ``harness`` lacks is satisfied, and anything else is
    ``unsupported``. A rule Mars attached a note to is at best ``partial``, with the note. Lines
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
        if rule is not None and rule.origin == PLATFORM_ORIGIN and line.status == "native":
            line = ReportLine(line.element, "platform", line.note)
        elif rule is not None and rule.note and line.status == "native":
            # Mars notes a rule only when its native spelling does not carry the rule exactly
            # (a shared permission name, an overlap, a shadowed scope), so it is not native.
            note = rule.note if not line.note else f"{line.note}; {rule.note}"
            line = ReportLine(line.element, "partial", note)
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


def _allowlist_denies(rules: tuple[ToolRule, ...]) -> tuple[ToolRule, ...]:
    """The unscoped deny each per-capability allowlist means, in policy order.

    A capability with scoped allows and no unscoped rule refuses every call they don't
    match. A ``*`` rule decides those calls itself, so it ends every such allowlist. The
    deny names the tools the capability's scoped rules concern (their ``tools``): for an
    unscoped rule the permission spelling and the tool are the same name. A capability Mars
    could not name keeps its rules' status, so the report states the same gap.
    """

    if unscoped_action(rules, "*") is not None:
        return ()
    first_allow: dict[str, ToolRule] = {}
    for rule in rules:
        if rule.capability is not None and rule.pattern is not None and rule.action == "allow":
            first_allow.setdefault(rule.capability, rule)
    denies: list[ToolRule] = []
    for capability, allow in first_allow.items():
        if unscoped_action(rules, capability) is not None:
            continue
        natives = tuple(
            dict.fromkeys(
                tool
                for rule in rules
                if rule.capability == capability and rule.pattern is not None
                for tool in rule.tools
            )
        )
        denies.append(
            ToolRule(
                capability=capability,
                action="deny",
                native=natives,
                native_tools=natives,
                native_status="known" if natives else allow.native_status,
                origin=ALLOWLIST_ORIGIN,
            )
        )
    return tuple(denies)


def _resolves_by_specificity(rule: ToolRule) -> bool:
    return (
        rule.capability not in {None, "*", "mcp"}
        and rule.pattern is None
        and rule.origin != PLATFORM_ORIGIN
        and bool(rule.native)
    )


def _resolve_allowlist_overlap(
    denies: tuple[ToolRule, ...], rules: tuple[ToolRule, ...]
) -> tuple[tuple[ToolRule, ...], tuple[ToolRule, ...]]:
    """Resolve each allowlist deny against the other unscoped rules, as Mars resolves its own.

    For the natives two unscoped rules share, the narrower capability wins when their actions
    differ; narrower means its natives are a strict subset of the other's. Mars did this for
    every rule it wrote, but it never saw these denies: without it, ``edit: allow`` beside
    ``write(src/*): allow`` keeps naming ``Write`` unscoped, and every write runs. Mars's own
    rules are not re-resolved against each other. Equal sets stay in conflict, as in Mars.
    Every participant is unscoped, so its natives are its tools and a narrowing drops both.
    """

    participants = [rule for rule in (*denies, *rules) if _resolves_by_specificity(rule)]
    allowlist = [rule for rule in denies if _resolves_by_specificity(rule)]

    def narrowed(rule: ToolRule, against: list[ToolRule]) -> ToolRule:
        if not _resolves_by_specificity(rule):
            return rule
        broad = rule.native
        kept = tuple(
            native
            for native in broad
            if not any(
                other.action != rule.action
                and len(other.native) < len(broad)
                and all(name in broad for name in other.native)
                and native in other.native
                for other in against
            )
        )
        if kept == broad:
            return rule
        return rule.model_copy(update={"native": kept, "native_tools": kept})

    return (
        tuple(narrowed(deny, participants) for deny in denies),
        tuple(narrowed(rule, allowlist) for rule in rules),
    )


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
    re-resolves precedence. Each per-capability allowlist gains
    the unscoped deny it means (see ``PermissionPolicy``), resolved against the other
    unscoped rules by specificity, so every compile reads the same rules.
    """

    denies, resolved = _resolve_allowlist_overlap(_allowlist_denies(rules), rules)
    return PermissionPolicy(
        # First, as the broad rule its scoped allows are exceptions to: OpenCode orders equally
        # specific rules by policy order, and this keeps its config what it was before the
        # allowlist became a rule.
        rules=(*denies, *resolved),
        platform_rules=platform_rules,
        sandbox=_parse_sandbox_value(sandbox),
        network=network,
        sandbox_write_roots=tuple(dict.fromkeys(root for root in sandbox_write_roots if root)),
        approval=_parse_approval_value(approval),
    )


__all__ = [
    "ALLOWLIST_ORIGIN",
    "CONSTRAINED_SANDBOX_MODES",
    "SANDBOX_MODE_WEAKNESS",
    "Allow",
    "ApprovalMode",
    "CommandMatch",
    "EnforcementReport",
    "Host",
    "Hosts",
    "NetworkPolicy",
    "NetworkProjection",
    "PermissionPolicy",
    "PlatformCommand",
    "PlatformCommandRole",
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
