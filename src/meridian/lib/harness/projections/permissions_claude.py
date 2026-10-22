"""Compile Meridian's permission policy to Claude's native launch controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.claude_preflight import CLAUDE_PARENT_ALLOWED_TOOLS_FLAG
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty, split_csv_entries
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    PlatformRule,
    PlatformRuleKind,
    ReportLine,
    allowlist_element,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule

_PLATFORM_DENY_TOOLS: dict[str, tuple[str, ...]] = {
    "agent": ("Agent",),
    "task": ("TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate"),
}
_BUILTIN_AGENT_PATTERNS: tuple[str, ...] = (
    "Explore",
    "Plan",
    "General-purpose",
    "general-purpose",
)
_ALLOWED_TOOL_FLAGS = frozenset(("--allowedTools", "--allowed-tools"))
_DISALLOWED_TOOL_FLAGS = frozenset(("--disallowedTools", "--disallowed-tools"))
_EXTERNAL_ALLOW_ELEMENT = "source: Claude parent-project and passthrough tool allows"


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
    permission_mode: Literal["acceptEdits", "manual", "dontAsk"] | None = None
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
class _ExternalTools:
    passthrough_args: tuple[str, ...]
    parent_allowed: tuple[str, ...]
    passthrough_allowed: tuple[str, ...]
    passthrough_disallowed: tuple[str, ...]

    @property
    def allowed(self) -> tuple[str, ...]:
        return (*self.parent_allowed, *self.passthrough_allowed)


def _native_base(name: str) -> str:
    """Strip one native scope for ``--tools`` availability and comparisons."""

    return name.partition("(")[0].strip()


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


def _extract_external_tools(extra_args: tuple[str, ...]) -> _ExternalTools:
    remaining: list[str] = []
    parent_allowed: list[str] = []
    passthrough_allowed: list[str] = []
    passthrough_disallowed: list[str] = []
    index = 0
    while index < len(extra_args):
        token = extra_args[index]
        if token == CLAUDE_PARENT_ALLOWED_TOOLS_FLAG:
            if index + 1 < len(extra_args):
                parent_allowed.extend(split_csv_entries(extra_args[index + 1]))
                index += 2
                continue
            index += 1
            continue
        if token.startswith(f"{CLAUDE_PARENT_ALLOWED_TOOLS_FLAG}="):
            parent_allowed.extend(split_csv_entries(token.partition("=")[2]))
            index += 1
            continue

        matched = False
        for flags, target in (
            (_ALLOWED_TOOL_FLAGS, passthrough_allowed),
            (_DISALLOWED_TOOL_FLAGS, passthrough_disallowed),
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

    return _ExternalTools(
        passthrough_args=tuple(remaining),
        parent_allowed=tuple(dedupe_nonempty(parent_allowed)),
        passthrough_allowed=tuple(dedupe_nonempty(passthrough_allowed)),
        passthrough_disallowed=tuple(dedupe_nonempty(passthrough_disallowed)),
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


def _allowlisted_native_bases(policy: PermissionPolicy) -> frozenset[str]:
    capabilities = frozenset(policy.allowlisted_capabilities)
    return frozenset(
        _native_base(native)
        for rule in policy.rules
        if rule.capability in capabilities
        for native in rule.native
    )


def _filter_external_allows(
    policy: PermissionPolicy,
    external: _ExternalTools,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    allowed = external.allowed
    if policy.is_allowlist:
        return (), allowed

    dropped_bases = _allowlisted_native_bases(policy)
    generic_agent_denied = "agent" in _platform_denied_capabilities(policy)
    builtins_denied = _platform_rule(policy, "builtin-agents") is not None
    kept: list[str] = []
    dropped: list[str] = []
    for native in allowed:
        drop = _native_base(native) in dropped_bases
        if generic_agent_denied and _is_agent_tool(native):
            drop = drop or builtins_denied or not _builtin_agent_tool(native)
        (dropped if drop else kept).append(native)
    return tuple(dedupe_nonempty(kept)), tuple(dedupe_nonempty(dropped))


def _compile_channels(
    policy: PermissionPolicy,
    effective: tuple[_Effective, ...],
    external: _ExternalTools,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    settings_allow: list[str] = []
    settings_deny: list[str] = []
    settings_ask: list[str] = []
    disallowed: list[str] = []

    for entry in effective:
        rule = entry.rule
        if rule.is_wildcard or rule.native_status != "known":
            continue
        if entry.action == "deny" and rule.pattern is None:
            disallowed.extend(rule.native)
        elif entry.action == "deny":
            settings_deny.extend(rule.native)
        elif entry.action == "ask":
            settings_ask.extend(rule.native)
        else:
            settings_allow.extend(rule.native)

    denied = _platform_denied_capabilities(policy)
    for capability in denied:
        if capability == "agent" and not _broad_agent_deny_is_native(policy):
            continue
        disallowed.extend(_PLATFORM_DENY_TOOLS[capability])
    if _platform_rule(policy, "builtin-agents") is not None:
        disallowed.extend(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)

    settings_allow.extend(_completion_tools(policy))
    external_allowed, _ = _filter_external_allows(policy, external)
    settings_allow.extend(external_allowed)
    disallowed.extend(external.passthrough_disallowed)
    denied = tuple(dedupe_nonempty(disallowed))
    compiled_deny = tuple(dedupe_nonempty(settings_deny))
    compiled_ask = tuple(dedupe_nonempty(settings_ask))
    blockers = frozenset((*denied, *compiled_deny, *compiled_ask))
    settings_allow = [
        native
        for native in settings_allow
        if native not in blockers and _native_base(native) not in blockers
    ]
    return (
        tuple(dedupe_nonempty(settings_allow)),
        compiled_deny,
        compiled_ask,
        denied,
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


def _compile_sandbox(policy: PermissionPolicy) -> tuple[bool | None, tuple[str, ...], bool]:
    if policy.sandbox == "default":
        return None, (), False
    if policy.sandbox == "danger-full-access":
        return False, (), False
    return (
        True,
        policy.sandbox_write_roots if policy.sandbox == "read-only" else (),
        policy.approval == "auto"
        and not (policy.is_allowlist or policy.allowlisted_capabilities)
        and not any(rule.action == "ask" for rule in policy.rules),
    )


def _compile_approval(
    policy: PermissionPolicy, *, has_ask_rules: bool
) -> tuple[bool, Literal["acceptEdits", "manual", "dontAsk"] | None]:
    if policy.is_allowlist or policy.allowlisted_capabilities:
        return False, "dontAsk"
    # A permission-mode bypass would turn a profile's ask into an implicit allow.
    # Keep the native rule authoritative; print mode denies it through
    # ``--permission-prompts none`` and a primary can still ask its human.
    if has_ask_rules:
        return False, "manual"
    if policy.approval in ("yolo", "never"):
        return True, None
    if policy.approval == "auto":
        return False, "acceptEdits"
    if policy.approval == "confirm":
        return False, "manual"
    return False, None


def _headless_passthrough(
    passthrough_args: tuple[str, ...], *, interactive: bool, has_ask_rules: bool
) -> tuple[str, ...]:
    if interactive or not has_ask_rules:
        return passthrough_args
    return tuple(
        arg
        for arg in passthrough_args
        if arg != "--dangerously-skip-permissions"
        and not arg.startswith("--dangerously-skip-permissions=")
    )


def _tool_rule_line(entry: _Effective) -> ReportLine | None:
    rule = entry.rule
    if rule.native_status != "known":
        return None
    element = rule_element(rule)
    if entry.overridden:
        return ReportLine(element, "platform", f"overridden to {entry.action} by a platform rule")
    if rule.is_wildcard:
        if rule.action == "deny":
            return ReportLine(element, "native", "--tools limits Claude to the named tools")
        return ReportLine(element, "unsupported", "Claude has no native wildcard permission rule")
    if not rule.native:
        return None
    names = ",".join(rule.native)
    if entry.action == "deny" and rule.pattern is None:
        return ReportLine(element, "native", f"--disallowedTools {names}")
    return ReportLine(
        element,
        "native",
        f"--settings permissions.{entry.action} {names}",
    )


def _allowlist_lines(policy: PermissionPolicy) -> list[ReportLine]:
    return [
        ReportLine(
            allowlist_element(capability),
            "partial",
            "--permission-mode dontAsk with scoped permissions.allow; Claude also "
            "auto-approves Bash calls it classifies as read-only and Read/Grep/Glob calls",
        )
        for capability in policy.allowlisted_capabilities
    ]


def _platform_lines(
    policy: PermissionPolicy, permissions: ClaudePermissions
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for platform in policy.platform_rules:
        if platform.kind == "native-subagents":
            names = [
                native
                for capability in platform.capabilities
                if capability != "agent" or _broad_agent_deny_is_native(policy)
                for native in _PLATFORM_DENY_TOOLS[capability]
            ]
            if "agent" in platform.capabilities and not _broad_agent_deny_is_native(policy):
                prefix = "--disallowedTools " + ",".join(names) + "; " if names else ""
                lines.append(
                    ReportLine(
                        platform_element(platform),
                        "partial",
                        prefix
                        + "generic Agent cannot be denied without also disabling "
                        "allowed built-in agents",
                    )
                )
            else:
                lines.append(
                    ReportLine(
                        platform_element(platform),
                        "platform",
                        "--disallowedTools " + ",".join(names),
                    )
                )
        elif platform.kind == "builtin-agents":
            names = ",".join(f"Agent({pattern})" for pattern in _BUILTIN_AGENT_PATTERNS)
            lines.append(
                ReportLine(
                    platform_element(platform), "platform", f"--disallowedTools {names}"
                )
            )
        elif platform.kind == "completion-commands":
            natives = _completion_tools(policy)
            emitted = frozenset(permissions.settings.allow)
            omitted = [native for native in natives if native not in emitted]
            note = "--settings permissions.allow " + ",".join(natives)
            allowed_bases = {_native_base(native) for native in policy.allowed_natives}
            if omitted:
                lines.append(
                    ReportLine(
                        platform_element(platform),
                        "partial",
                        "completion allow omitted; profile/native precedence blocks "
                        + ", ".join(omitted),
                    )
                )
            elif policy.is_allowlist and "Bash" not in allowed_bases:
                lines.append(
                    ReportLine(
                        platform_element(platform),
                        "partial",
                        f"{note}; adding Bash to --tools also lets dontAsk auto-approve "
                        "other commands Claude classifies as read-only",
                    )
                )
            else:
                lines.append(ReportLine(platform_element(platform), "platform", note))
    return lines


def _external_line(
    policy: PermissionPolicy,
    external: _ExternalTools,
    permissions: ClaudePermissions,
) -> ReportLine | None:
    if not external.allowed:
        return None
    kept, dropped = _filter_external_allows(policy, external)
    emitted = frozenset(permissions.settings.allow)
    denied = tuple(native for native in kept if native not in emitted)
    kept = tuple(native for native in kept if native in emitted)
    dropped = tuple(dedupe_nonempty((*dropped, *denied)))
    note_parts: list[str] = []
    if kept:
        note_parts.append("--settings permissions.allow " + ",".join(kept))
    if dropped:
        note_parts.append("dropped by profile/platform policy: " + ",".join(dropped))
    return ReportLine(
        _EXTERNAL_ALLOW_ELEMENT,
        "partial" if dropped else "native",
        "; ".join(note_parts),
    )


def _claude_report(
    policy: PermissionPolicy,
    permissions: ClaudePermissions,
    effective: tuple[_Effective, ...],
    external: _ExternalTools,
) -> EnforcementReport:
    lines = [line for entry in effective if (line := _tool_rule_line(entry)) is not None]
    lines.extend(_allowlist_lines(policy))
    lines.extend(_platform_lines(policy, permissions))
    external_line = _external_line(policy, external, permissions)
    if external_line is not None:
        lines.append(external_line)
    if policy.sandbox != "default":
        settings = "--settings sandbox.enabled=" + str(
            permissions.settings.sandbox_enabled
        ).lower()
        if policy.sandbox == "read-only":
            settings += " with filesystem.denyWrite on workspace roots"
        status: Literal["native", "partial"] = "native"
        if policy.sandbox != "danger-full-access":
            status = "partial"
            settings += "; confines Bash subprocesses only, not Edit/Write"
        lines.append(ReportLine(f"sandbox: {policy.sandbox}", status, settings))
    if policy.approval != "default":
        status: Literal["native", "partial"] = "native"
        if permissions.skip_permission_prompts:
            note = "--dangerously-skip-permissions"
        elif permissions.permission_mode is not None:
            note = f"--permission-mode {permissions.permission_mode}"
        else:
            note = "Claude default"
        if policy.is_allowlist or policy.allowlisted_capabilities:
            status = "partial"
            note += "; the allowlist remains authoritative"
        elif permissions.settings.ask and policy.approval in ("auto", "yolo", "never"):
            status = "partial"
            note += "; ask rules remain authoritative instead of being bypassed"
        lines.append(ReportLine(f"approval: {policy.approval}", status, note))
    return complete_report(policy, lines, harness="Claude")


def compile_claude_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[ClaudePermissions, EnforcementReport]:
    """Compile one spec without deriving a Claude name from a capability."""

    policy = spec.permission_policy
    effective = _effective_rules(policy)
    external = _extract_external_tools(spec.extra_args)
    settings_allow, settings_deny, settings_ask, disallowed = _compile_channels(
        policy, effective, external
    )
    sandbox_enabled, sandbox_deny_write, auto_allow_bash = _compile_sandbox(policy)
    skip_prompts, permission_mode = _compile_approval(
        policy, has_ask_rules=bool(settings_ask)
    )
    permissions = ClaudePermissions(
        skip_permission_prompts=skip_prompts,
        permission_mode=permission_mode,
        permission_prompts=None if spec.interactive else "none",
        restrict_tools=policy.is_allowlist,
        available_tools=_available_tools(policy),
        disallowed_tools=disallowed,
        settings=ClaudeSettings(
            allow=settings_allow,
            deny=settings_deny,
            ask=settings_ask,
            sandbox_enabled=sandbox_enabled,
            sandbox_deny_write=sandbox_deny_write,
            auto_allow_bash_if_sandboxed=auto_allow_bash,
        ),
        passthrough_args=_headless_passthrough(
            external.passthrough_args,
            interactive=spec.interactive,
            has_ask_rules=bool(settings_ask),
        ),
    )
    return permissions, _claude_report(policy, permissions, effective, external)


__all__ = ["ClaudePermissions", "ClaudeSettings", "compile_claude_permissions"]
