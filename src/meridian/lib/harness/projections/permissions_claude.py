"""Compile Meridian's permission policy to Claude's native launch controls.

Claude checks deny rules, then ask rules, then allow rules, whatever their specificity, so a
broad deny or ask shadows every narrower allow. A capability whose unmatched calls must not
run freely (an allowlist, or a plain deny or ask beside scoped allows) is therefore gated by
the permission mode instead of a broad rule: a mode in which unmatched calls need approval,
which a headless spawn cannot give. Every report line is read back from the emitted
channels, so a rule that order shadows, or that the compile left out, says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal, cast

from meridian.lib.harness.claude_preflight import CLAUDE_SETTINGS_ALLOWS_FLAG
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty, split_csv_entries
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    PlatformRule,
    PlatformRuleKind,
    ReportLine,
    ReportStatus,
    allowlist_element,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule

type PermissionMode = Literal["acceptEdits", "manual", "dontAsk"]

_PLATFORM_DENY_TOOLS: dict[str, tuple[str, ...]] = {
    "agent": ("Agent", "SendMessage", "ListAgents"),
    "task": ("TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"),
}
_BUILTIN_AGENT_PATTERNS: tuple[str, ...] = (
    "Explore",
    "Plan",
    "General-purpose",
    "general-purpose",
)
# acceptEdits approves unmatched calls of these on its own (edits, and file commands such as
# touch, rm and redirects inside the working directories), so it cannot gate them.
_ACCEPT_EDITS_CAPABILITIES = frozenset(("bash", "edit", "write", "notebook"))
# Claude runs a read-only class of these without asking in every permission mode.
_READ_ONLY_CLASS_CAPABILITIES = frozenset(("bash", "read", "glob", "grep", "list"))
_ALLOWED_TOOL_FLAGS = frozenset(("--allowedTools", "--allowed-tools"))
_DISALLOWED_TOOL_FLAGS = frozenset(("--disallowedTools", "--disallowed-tools"))
_BYPASS_FLAG = "--dangerously-skip-permissions"
_ALLOW_BYPASS_FLAG = "--allow-dangerously-skip-permissions"
_MODE_FLAG = "--permission-mode"
_SETTINGS_FLAG = "--settings"
_PASSTHROUGH_ALLOW_ELEMENT = "source: passthrough --allowedTools"


@dataclass(frozen=True)
class ClaudeSettings:
    """The subset of Claude ``--settings`` Meridian owns for one launch."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    sandbox_enabled: bool | None = None
    sandbox_deny_write: tuple[str, ...] = ()
    auto_allow_bash_if_sandboxed: bool = False

    @property
    def empty(self) -> bool:
        return not (
            self.allow
            or self.deny
            or self.ask
            or self.sandbox_enabled is not None
            or self.auto_allow_bash_if_sandboxed
        )


@dataclass(frozen=True)
class ClaudePermissions:
    """Complete native permission projection consumed by ``project_claude.py``."""

    skip_permission_prompts: bool = False
    permission_mode: PermissionMode | None = None
    permission_prompts: Literal["none"] | None = None
    restrict_tools: bool = False
    available_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    settings: ClaudeSettings = ClaudeSettings()
    passthrough_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Effective:
    rule: ToolRule
    action: ToolAction

    @property
    def overridden(self) -> bool:
        return self.action != self.rule.action


@dataclass(frozen=True)
class _Passthrough:
    """Passthrough args to forward, and the permission inputs the compile consumed."""

    args: tuple[str, ...]
    allowed: tuple[str, ...]
    disallowed: tuple[str, ...]
    settings_allows: tuple[tuple[str, tuple[str, ...]], ...]

    def has(self, flag: str) -> bool:
        return any(token == flag or token.startswith(f"{flag}=") for token in self.args)

    def value(self, flag: str) -> str | None:
        """The last value given to ``flag``; Claude takes the last occurrence."""

        found: str | None = None
        for index, token in enumerate(self.args):
            if token == flag and index + 1 < len(self.args):
                found = self.args[index + 1]
            elif token.startswith(f"{flag}="):
                found = token.partition("=")[2]
        return found

    @property
    def overrides_mode(self) -> bool:
        return self.has(_BYPASS_FLAG) or self.value(_MODE_FLAG) is not None


@dataclass(frozen=True)
class _Gate:
    """A capability the permission mode restricts because no broad rule can."""

    capability: str | None
    base: str
    action: ToolAction | None  # the plain rule the mode stands in for; None for an allowlist


@dataclass(frozen=True)
class _Channels:
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    ask: tuple[str, ...]
    disallowed: tuple[str, ...]
    gates: tuple[_Gate, ...]

    @property
    def gated_bases(self) -> frozenset[str]:
        return frozenset(gate.base for gate in self.gates)


def _native_base(name: str) -> str:
    """Strip one native scope for ``--tools`` availability and comparisons."""

    return name.partition("(")[0].strip()


def _native_scope(name: str) -> str | None:
    if "(" not in name:
        return None
    return name.partition("(")[2].removesuffix(")")


def _is_agent_tool(name: str) -> bool:
    return _native_base(name) == "Agent"


def _builtin_agent_tool(name: str) -> bool:
    return name in {f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS}


def _platform_rule(policy: PermissionPolicy, kind: PlatformRuleKind) -> PlatformRule | None:
    return next((rule for rule in policy.platform_rules if rule.kind == kind), None)


def _platform_denied_capabilities(policy: PermissionPolicy) -> tuple[str, ...]:
    rule = _platform_rule(policy, "native-subagents")
    return () if rule is None else rule.capabilities


def _broad_agent_deny_is_native(policy: PermissionPolicy) -> bool:
    """A broad Agent deny would also disable built-ins the user explicitly enabled."""

    return "agent" in _platform_denied_capabilities(policy) and (
        _platform_rule(policy, "builtin-agents") is not None
    )


def _platform_deny_tools(policy: PermissionPolicy, capability: str) -> tuple[str, ...]:
    names = _PLATFORM_DENY_TOOLS[capability]
    if capability == "agent" and not _broad_agent_deny_is_native(policy):
        return tuple(name for name in names if name != "Agent")
    return names


def _effective_rules(policy: PermissionPolicy) -> tuple[_Effective, ...]:
    denied = _platform_denied_capabilities(policy)
    deny_builtins = _platform_rule(policy, "builtin-agents") is not None
    broad_agent_deny = _broad_agent_deny_is_native(policy)
    effective: list[_Effective] = []
    for rule in policy.rules:
        action = rule.action
        if (
            (rule.capability == "agent" and broad_agent_deny)
            or (rule.capability == "task" and "task" in denied)
            or (
                rule.capability == "agent"
                and rule.pattern in _BUILTIN_AGENT_PATTERNS
                and deny_builtins
            )
        ):
            action = "deny"
        effective.append(_Effective(rule, action))
    return tuple(effective)


def _take_variadic_tool_values(args: tuple[str, ...], start: int) -> tuple[list[str], int]:
    values: list[str] = []
    index = start
    while index < len(args) and not args[index].startswith("-"):
        values.extend(split_csv_entries(args[index]))
        index += 1
    return values, index


def _parse_settings_allows(raw: str) -> list[tuple[str, tuple[str, ...]]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for item in cast("list[object]", payload):
        if not isinstance(item, list) or len(cast("list[object]", item)) != 2:
            continue
        path, allows = cast("list[object]", item)
        if isinstance(path, str) and isinstance(allows, list):
            entries = tuple(
                entry for entry in cast("list[object]", allows) if isinstance(entry, str)
            )
            parsed.append((path, entries))
    return parsed


def _read_passthrough(extra_args: tuple[str, ...]) -> _Passthrough:
    remaining: list[str] = []
    allowed: list[str] = []
    disallowed: list[str] = []
    settings_allows: list[tuple[str, tuple[str, ...]]] = []
    index = 0
    while index < len(extra_args):
        token = extra_args[index]
        if token == CLAUDE_SETTINGS_ALLOWS_FLAG:
            if index + 1 < len(extra_args):
                settings_allows.extend(_parse_settings_allows(extra_args[index + 1]))
            index += 2
            continue

        matched = False
        for flags, target in (
            (_ALLOWED_TOOL_FLAGS, allowed),
            (_DISALLOWED_TOOL_FLAGS, disallowed),
        ):
            if token in flags:
                values, next_index = _take_variadic_tool_values(extra_args, index + 1)
                target.extend(values)
                index = next_index
                matched = True
                break
            matching_prefix = next(
                (f"{flag}=" for flag in flags if token.startswith(f"{flag}=")), None
            )
            if matching_prefix is not None:
                target.extend(split_csv_entries(token.removeprefix(matching_prefix)))
                index += 1
                matched = True
                break
        if matched:
            continue
        remaining.append(token)
        index += 1

    return _Passthrough(
        args=tuple(remaining),
        allowed=tuple(dedupe_nonempty(allowed)),
        disallowed=tuple(dedupe_nonempty(disallowed)),
        settings_allows=tuple(settings_allows),
    )


def _completion_tools(policy: PermissionPolicy) -> tuple[str, ...]:
    platform = _platform_rule(policy, "completion-commands")
    if platform is None:
        return ()
    return tuple(
        native
        for command in platform.commands
        for native in (f"Bash({command})", f"Bash({command} *)")
    )


def _filter_passthrough_allows(
    policy: PermissionPolicy,
    passthrough: _Passthrough,
    gated_bases: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split passthrough allows into kept and dropped: a profile allowlist owns its tools."""

    allowed = passthrough.allowed
    if policy.is_allowlist:
        return (), allowed

    generic_agent_denied = "agent" in _platform_denied_capabilities(policy)
    builtins_denied = _platform_rule(policy, "builtin-agents") is not None
    kept: list[str] = []
    dropped: list[str] = []
    for native in allowed:
        drop = _native_base(native) in gated_bases
        if generic_agent_denied and _is_agent_tool(native):
            drop = drop or builtins_denied or not _builtin_agent_tool(native)
        (dropped if drop else kept).append(native)
    return tuple(dedupe_nonempty(kept)), tuple(dedupe_nonempty(dropped))


def _compile_channels(
    policy: PermissionPolicy,
    effective: tuple[_Effective, ...],
    passthrough: _Passthrough,
) -> _Channels:
    completion = _completion_tools(policy)
    profile_scoped_bases = {
        _native_base(native)
        for entry in effective
        if entry.action == "allow" and entry.rule.native_status == "known"
        for native in entry.rule.native
        if _native_scope(native) is not None
    }

    allow: list[str] = []
    deny: list[str] = []
    ask: list[str] = []
    plain: list[tuple[_Effective, str]] = []
    for entry in effective:
        rule = entry.rule
        if rule.is_wildcard or rule.native_status != "known":
            continue
        for native in rule.native:
            if entry.action == "allow":
                allow.append(native)
            elif _native_scope(native) is not None:
                (deny if entry.action == "deny" else ask).append(native)
            else:
                plain.append((entry, native))

    # A plain deny or ask beside scoped allows of the same tool would shadow them.
    gates: dict[tuple[str | None, str], _Gate] = {
        (entry.rule.capability, native): _Gate(entry.rule.capability, native, entry.action)
        for entry, native in plain
        if native in profile_scoped_bases
    }
    for capability in policy.allowlisted_capabilities:
        for entry in effective:
            if entry.rule.capability != capability or entry.action != "allow":
                continue
            for native in entry.rule.native:
                if _native_scope(native) is not None:
                    base = _native_base(native)
                    gates.setdefault((capability, base), _Gate(capability, base, None))
    # The completion allows alone never change the profile's mode: they gate a plain rule
    # only where the mode already makes unmatched calls ask. Elsewhere the plain rule
    # stays and shadows them, and the completion line says so.
    required_mode = _gate_mode(policy, tuple(gates.values()))
    completion_gates_free = (
        policy.is_allowlist
        or required_mode == "manual"
        or (required_mode is None and policy.approval in ("default", "confirm"))
    )
    completion_bases = {_native_base(native) for native in completion}
    disallowed: list[str] = []
    for entry, native in plain:
        if (entry.rule.capability, native) in gates:
            continue
        if native in completion_bases and completion_gates_free:
            gates[(entry.rule.capability, native)] = _Gate(
                entry.rule.capability, native, entry.action
            )
        elif entry.action == "deny":
            disallowed.append(native)
        else:
            ask.append(native)

    for capability in _platform_denied_capabilities(policy):
        disallowed.extend(_platform_deny_tools(policy, capability))
    if _platform_rule(policy, "builtin-agents") is not None:
        disallowed.extend(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)

    gate_tuple = tuple(gates.values())
    allow.extend(completion)
    kept, _ = _filter_passthrough_allows(
        policy, passthrough, frozenset(gate.base for gate in gate_tuple)
    )
    allow.extend(kept)
    disallowed.extend(passthrough.disallowed)
    denied = tuple(dedupe_nonempty(disallowed))
    compiled_deny = tuple(dedupe_nonempty(deny))
    compiled_ask = tuple(dedupe_nonempty(ask))
    blockers = frozenset((*denied, *compiled_deny, *compiled_ask))
    return _Channels(
        allow=tuple(
            native
            for native in dedupe_nonempty(allow)
            if native not in blockers and _native_base(native) not in blockers
        ),
        deny=compiled_deny,
        ask=compiled_ask,
        disallowed=denied,
        gates=gate_tuple,
    )


def _available_tools(policy: PermissionPolicy) -> tuple[str, ...]:
    if not policy.is_allowlist:
        return ()
    return tuple(
        dedupe_nonempty(
            _native_base(native)
            for native in (*policy.allowed_natives, *_completion_tools(policy))
        )
    )


def _compile_sandbox(
    policy: PermissionPolicy, channels: _Channels
) -> tuple[bool | None, tuple[str, ...], bool]:
    if policy.sandbox == "default":
        return None, (), False
    if policy.sandbox == "danger-full-access":
        return False, (), False
    # Sandbox auto-approval accepts unmatched Bash before any mode can refuse it.
    return (
        True,
        policy.sandbox_write_roots if policy.sandbox == "read-only" else (),
        policy.approval == "auto"
        and not policy.is_allowlist
        and not channels.gates
        and not channels.ask,
    )


def _approval_mode(policy: PermissionPolicy) -> tuple[bool, PermissionMode | None]:
    """What ``approval`` alone compiles to."""

    if policy.approval in ("yolo", "never"):
        return True, None
    if policy.approval == "auto":
        return False, "acceptEdits"
    if policy.approval == "confirm":
        return False, "manual"
    return False, None


def _gate_mode(policy: PermissionPolicy, gates: tuple[_Gate, ...]) -> PermissionMode | None:
    """The mode that keeps every gate authoritative without a whole-policy allowlist.

    dontAsk would refuse every other tool too. acceptEdits keeps edits running, so it is used
    when ``approval`` would approve them and no gated capability is one it approves itself.
    """

    if not gates:
        return None
    if policy.approval in ("auto", "never", "yolo") and all(
        gate.capability is not None and gate.capability not in _ACCEPT_EDITS_CAPABILITIES
        for gate in gates
    ):
        return "acceptEdits"
    return "manual"


def _policy_mode(
    policy: PermissionPolicy, channels: _Channels
) -> tuple[bool, PermissionMode | None]:
    """The mode the profile needs: one that keeps its allowlists and gates authoritative."""

    if policy.is_allowlist:
        return False, "dontAsk"
    mode = _gate_mode(policy, channels.gates)
    return (False, mode) if mode is not None else _approval_mode(policy)


def _channel_of(native: str, permissions: ClaudePermissions) -> str | None:
    settings = permissions.settings
    if native in permissions.disallowed_tools:
        return "--disallowedTools"
    if native in settings.deny:
        return "--settings permissions.deny"
    if native in settings.ask:
        return "--settings permissions.ask"
    if native in settings.allow:
        return "--settings permissions.allow"
    return None


def _shadow(native: str, permissions: ClaudePermissions) -> str | None:
    """The emitted deny or ask Claude checks before this allow and that covers it."""

    base, scope = _native_base(native), _native_scope(native)
    for blocker in (
        *permissions.disallowed_tools,
        *permissions.settings.deny,
        *permissions.settings.ask,
    ):
        if _native_base(blocker) != base:
            continue
        blocker_scope = _native_scope(blocker)
        if blocker_scope is None or (scope is not None and fnmatchcase(scope, blocker_scope)):
            return blocker
    return None


def _render_placed(placed: list[tuple[str, str]]) -> str:
    by_channel: dict[str, list[str]] = {}
    for channel, native in placed:
        by_channel.setdefault(channel, []).append(native)
    return "; ".join(f"{channel} {','.join(names)}" for channel, names in by_channel.items())


def _gate_note(
    permissions: ClaudePermissions,
    *,
    capability: str | None,
    bases: list[str],
    interactive: bool,
) -> str:
    names = ", ".join(bases)
    mode = permissions.permission_mode
    if mode is None:
        note = f"a passthrough permission flag decides unmatched {names} calls"
    elif mode == "dontAsk":
        note = f"dontAsk refuses unmatched {names} calls"
    else:
        note = f"unmatched {names} calls need approval under --permission-mode {mode}, " + (
            "so Claude prompts the human"
            if interactive
            else "and a spawn has no one to ask, so Claude refuses them"
        )
    if capability is None or capability in _READ_ONLY_CLASS_CAPABILITIES:
        note += "; Claude still runs calls it classifies as read-only without asking"
    if mode == "acceptEdits" and capability == "bash":
        note += (
            "; acceptEdits also runs file commands (mkdir, cp, mv, rm, sed -i, output "
            "redirection) on paths inside the working directories"
        )
    return note


def _gate_is_exact(gate: _Gate, permissions: ClaudePermissions, *, interactive: bool) -> bool:
    if permissions.permission_mode is None:
        return False
    if gate.capability is None or gate.capability in _READ_ONLY_CLASS_CAPABILITIES:
        return False
    # A primary's human answers the prompt: right for ask, a widening for deny or allowlist.
    return not interactive or gate.action == "ask"


def _tool_rule_line(
    entry: _Effective,
    permissions: ClaudePermissions,
    channels: _Channels,
    *,
    interactive: bool,
) -> ReportLine | None:
    rule = entry.rule
    if rule.native_status != "known":
        return None
    element = rule_element(rule)
    if entry.overridden:
        return ReportLine(element, "platform", f"overridden to {entry.action} by a platform rule")
    if rule.is_wildcard:
        if rule.action == "deny" and permissions.restrict_tools:
            note = f"--tools {','.join(permissions.available_tools)} and dontAsk"
            if "Skill" not in permissions.available_tools:
                note += "; Skill is not in --tools, so on-demand skills cannot load"
            note += "; MCP tools stay listed and dontAsk refuses those not allowed"
            return ReportLine(element, "native", note)
        return ReportLine(element, "unsupported", "Claude has no native wildcard permission rule")
    if not rule.native:
        return None

    placed: list[tuple[str, str]] = []
    notes: list[str] = []
    gates = {gate.base: gate for gate in channels.gates if gate.action is not None}
    gated = [native for native in rule.native if entry.action != "allow" and native in gates]
    partial = not all(
        _gate_is_exact(gates[native], permissions, interactive=interactive) for native in gated
    )
    if gated:
        notes.append(
            f"no plain {', '.join(gated)} {entry.action} rule, since Claude checks it before "
            "the scoped allows; "
            + _gate_note(
                permissions, capability=rule.capability, bases=gated, interactive=interactive
            )
        )
    for native in rule.native:
        if native in gated:
            continue
        channel = _channel_of(native, permissions)
        blocker = _shadow(native, permissions) if entry.action == "allow" else None
        if channel is None:
            notes.append(
                f"{native} omitted: Claude checks {blocker} first"
                if blocker is not None
                else f"{native} omitted"
            )
            partial = True
            continue
        placed.append((channel, native))
        if blocker is not None:
            notes.append(f"Claude checks {blocker} first, so {native} never applies")
            partial = True
        elif (
            entry.action == "allow"
            and permissions.restrict_tools
            and _native_base(native) not in permissions.available_tools
        ):
            notes.append(f"{_native_base(native)} is not in --tools")
            partial = True
        if entry.action == "ask" and permissions.permission_mode == "dontAsk" and interactive:
            notes.append("dontAsk refuses it instead of asking the human")
            partial = True
    if rule.is_legacy:
        notes.append("emitted as an older build wrote it; Claude ignores a name that is no tool")
        partial = True
    note = "; ".join(part for part in (_render_placed(placed), *notes) if part)
    return ReportLine(element, "partial" if partial else "native", note)


def _allowlist_lines(
    policy: PermissionPolicy,
    permissions: ClaudePermissions,
    channels: _Channels,
    *,
    interactive: bool,
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for capability in policy.allowlisted_capabilities:
        gates = [
            gate
            for gate in channels.gates
            if gate.capability == capability and gate.action is None
        ]
        if not gates:
            lines.append(
                ReportLine(
                    allowlist_element(capability),
                    "platform",
                    "platform rules deny every scoped allow",
                )
            )
            continue
        note = _gate_note(
            permissions,
            capability=capability,
            bases=[gate.base for gate in gates],
            interactive=interactive,
        )
        exact = all(_gate_is_exact(gate, permissions, interactive=interactive) for gate in gates)
        lines.append(
            ReportLine(
                allowlist_element(capability),
                "native" if exact else "partial",
                f"scoped allows in --settings permissions.allow; {note}",
            )
        )
    return lines


def _completion_line(
    policy: PermissionPolicy, platform: PlatformRule, permissions: ClaudePermissions
) -> ReportLine:
    natives = _completion_tools(policy)
    emitted = [native for native in natives if native in permissions.settings.allow]
    blocked: dict[str, list[str]] = {}
    for native in natives:
        blocker = _shadow(native, permissions)
        if native not in emitted or blocker is not None:
            blocked.setdefault(blocker or "", []).append(native)
    if blocked:
        problems = [
            f"{','.join(names)}: Claude checks {blocker} first" if blocker else
            f"{','.join(names)} omitted"
            for blocker, names in blocked.items()
        ]
        return ReportLine(platform_element(platform), "partial", "; ".join(problems))
    note = "--settings permissions.allow " + ",".join(emitted)
    profile_bases = {_native_base(native) for native in policy.allowed_natives}
    if permissions.restrict_tools and "Bash" not in profile_bases:
        return ReportLine(
            platform_element(platform),
            "partial",
            f"{note}; adding Bash to --tools for them also lets dontAsk run other commands "
            "Claude classifies as read-only",
        )
    return ReportLine(platform_element(platform), "platform", note)


def _platform_lines(
    policy: PermissionPolicy, permissions: ClaudePermissions
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for platform in policy.platform_rules:
        if platform.kind == "native-subagents":
            names = [
                name
                for capability in platform.capabilities
                for name in _platform_deny_tools(policy, capability)
                if name in permissions.disallowed_tools
            ]
            note = "--disallowedTools " + ",".join(names)
            if "agent" in platform.capabilities and not _broad_agent_deny_is_native(policy):
                lines.append(
                    ReportLine(
                        platform_element(platform),
                        "partial",
                        f"{note}; generic Agent cannot be denied without also disabling "
                        "allowed built-in agents",
                    )
                )
            else:
                lines.append(ReportLine(platform_element(platform), "platform", note))
        elif platform.kind == "builtin-agents":
            names = ",".join(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)
            lines.append(
                ReportLine(
                    platform_element(platform), "platform", f"--disallowedTools {names}"
                )
            )
        elif platform.kind == "completion-commands":
            lines.append(_completion_line(policy, platform, permissions))
    return lines


def _mode_note(skip_prompts: bool, mode: PermissionMode | None) -> str:
    if skip_prompts:
        return "--dangerously-skip-permissions"
    if mode is not None:
        return f"--permission-mode {mode}"
    return "Claude default"


def _approval_line(
    policy: PermissionPolicy,
    passthrough: _Passthrough,
    *,
    policy_mode: tuple[bool, PermissionMode | None],
) -> ReportLine:
    element = f"approval: {policy.approval}"
    compiled = _mode_note(*policy_mode)
    if passthrough.overrides_mode:
        return ReportLine(
            element,
            "partial",
            f"{compiled}, which a passthrough permission flag replaces",
        )
    if policy_mode == _approval_mode(policy):
        return ReportLine(element, "native", compiled)
    if policy.is_allowlist:
        return ReportLine(element, "partial", f"{compiled}; the allowlist decides every call")
    note = f"{compiled}, so unmatched calls of a restricted capability need approval"
    if policy_mode[1] == "manual":
        note += (
            "; acceptEdits would run its unmatched calls (edits, or file commands such as "
            "touch and rm), so edits the rules do not allow need approval too"
        )
    if policy.approval in ("never", "yolo"):
        note += (
            "; other calls that need approval (most Bash commands, web and MCP tools) are "
            "refused in a spawn and prompt in a primary"
        )
    return ReportLine(element, "partial", note)


def _source_lines(
    policy: PermissionPolicy,
    permissions: ClaudePermissions,
    channels: _Channels,
    passthrough: _Passthrough,
    *,
    policy_mode: tuple[bool, PermissionMode | None],
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    if passthrough.allowed:
        kept, dropped = _filter_passthrough_allows(policy, passthrough, channels.gated_bases)
        emitted = frozenset(permissions.settings.allow)
        dropped = (*dropped, *(native for native in kept if native not in emitted))
        kept = tuple(native for native in kept if native in emitted)
        parts: list[str] = []
        if kept:
            parts.append("--settings permissions.allow " + ",".join(kept))
        if dropped:
            parts.append("dropped by profile/platform policy: " + ",".join(dropped))
        lines.append(
            ReportLine(
                _PASSTHROUGH_ALLOW_ELEMENT, "partial" if dropped else "native", "; ".join(parts)
            )
        )
    if policy.is_allowlist or channels.gates:
        for path, allows in passthrough.settings_allows:
            widening = tuple(
                entry
                for entry in allows
                if not permissions.restrict_tools
                or _native_base(entry) in permissions.available_tools
                or entry.startswith("mcp__")
            )
            if widening:
                lines.append(
                    ReportLine(
                        f"source: Claude settings {path}",
                        "partial",
                        "Claude merges this file's permissions.allow into the launch, so "
                        "these run outside the profile allowlist: " + ",".join(widening),
                    )
                )
    if passthrough.has(_BYPASS_FLAG):
        lines.append(
            ReportLine(
                f"source: passthrough {_BYPASS_FLAG}",
                "partial",
                "bypasses Claude's permission mode, so the profile's mode and allowlists no "
                "longer decide unmatched calls; --tools, deny and ask rules still hold",
            )
        )
    if passthrough.has(_ALLOW_BYPASS_FLAG):
        lines.append(
            ReportLine(
                f"source: passthrough {_ALLOW_BYPASS_FLAG}",
                "partial",
                "lets the session switch to bypassPermissions, which overrides the profile's "
                "mode and allowlists",
            )
        )
    passthrough_mode = passthrough.value(_MODE_FLAG)
    if passthrough_mode is not None and policy_mode != (False, passthrough_mode):
        lines.append(
            ReportLine(
                f"source: passthrough {_MODE_FLAG}",
                "partial",
                f"--permission-mode {passthrough_mode} replaces the profile's "
                f"{_mode_note(*policy_mode)}; allowlists hold only where that mode refuses "
                "unmatched calls",
            )
        )
    if passthrough.has(_SETTINGS_FLAG) and not permissions.settings.empty:
        lines.append(
            ReportLine(
                f"source: passthrough {_SETTINGS_FLAG}",
                "partial",
                "Claude keeps only the last --settings, so Meridian's, which carries the "
                "profile policy, replaces it",
            )
        )
    return lines


def _sandbox_line(policy: PermissionPolicy, permissions: ClaudePermissions) -> ReportLine:
    settings = "--settings sandbox.enabled=" + str(permissions.settings.sandbox_enabled).lower()
    if policy.sandbox == "read-only":
        settings += " with filesystem.denyWrite on workspace roots"
    status: ReportStatus = "native"
    if policy.sandbox != "danger-full-access":
        status = "partial"
        settings += "; confines Bash subprocesses only, not Edit/Write"
    return ReportLine(f"sandbox: {policy.sandbox}", status, settings)


def compile_claude_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[ClaudePermissions, EnforcementReport]:
    """Compile one spec without deriving a Claude name from a capability."""

    policy = spec.permission_policy
    effective = _effective_rules(policy)
    passthrough = _read_passthrough(spec.extra_args)
    channels = _compile_channels(policy, effective, passthrough)
    sandbox_enabled, sandbox_deny_write, auto_allow_bash = _compile_sandbox(policy, channels)
    policy_mode = _policy_mode(policy, channels)
    # A passthrough mode is the launcher's own choice; Claude takes the last one, so
    # Meridian omits its mode rather than silently overriding it.
    skip_prompts, permission_mode = (False, None) if passthrough.overrides_mode else policy_mode
    permissions = ClaudePermissions(
        skip_permission_prompts=skip_prompts,
        permission_mode=permission_mode,
        permission_prompts=None if spec.interactive else "none",
        restrict_tools=policy.is_allowlist,
        available_tools=_available_tools(policy),
        disallowed_tools=channels.disallowed,
        settings=ClaudeSettings(
            allow=channels.allow,
            deny=channels.deny,
            ask=channels.ask,
            sandbox_enabled=sandbox_enabled,
            sandbox_deny_write=sandbox_deny_write,
            auto_allow_bash_if_sandboxed=auto_allow_bash,
        ),
        passthrough_args=passthrough.args,
    )

    interactive = spec.interactive
    lines = [
        line
        for entry in effective
        if (
            line := _tool_rule_line(entry, permissions, channels, interactive=interactive)
        )
        is not None
    ]
    lines.extend(_allowlist_lines(policy, permissions, channels, interactive=interactive))
    lines.extend(_platform_lines(policy, permissions))
    if policy.sandbox != "default":
        lines.append(_sandbox_line(policy, permissions))
    if policy.approval != "default":
        lines.append(
            _approval_line(policy, passthrough, policy_mode=policy_mode)
        )
    lines.extend(
        _source_lines(policy, permissions, channels, passthrough, policy_mode=policy_mode)
    )
    return permissions, complete_report(policy, lines, harness="Claude")


__all__ = ["ClaudePermissions", "ClaudeSettings", "compile_claude_permissions"]
