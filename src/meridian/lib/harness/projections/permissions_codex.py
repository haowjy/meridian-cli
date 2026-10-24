"""Codex's compile of the permission policy.

Codex takes a sandbox mode and an approval policy per launch. Unscoped allow and deny
rules for shell, web search, and image viewing compile to Codex's per-launch switches;
Codex has no native carrier for the other tool rules. The switches read semantic
capabilities, never harness-native names. The projectors spell this typed result for
``codex exec``, ``codex app-server``, and the thread payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_NETWORK_ACCESS_KEY,
    codex_key_addressable,
    codex_toml_literal,
    flatten_native_config,
    project_codex_native_config,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    ALLOWLIST_ORIGIN,
    CONSTRAINED_SANDBOX_MODES,
    Allow,
    ApprovalMode,
    EffectiveNetworkFacts,
    EnforcementReport,
    Hosts,
    NetworkProjection,
    PermissionPolicy,
    ReportLine,
    ReportStatus,
    SandboxMode,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import PLATFORM_ORIGIN, ToolRule

_APPROVAL_POLICY_BY_MODE: dict[ApprovalMode, str | None] = {
    "default": None,
    "auto": "on-request",
    # Codex 0.163 accepts only ``on-request`` and ``never``; ``untrusted`` is rejected
    # at startup, so ``confirm`` cannot be stricter than ``auto`` here.
    "confirm": "on-request",
    "never": "never",
    "yolo": "never",
}

_SANDBOX_MODE_BY_MODE: dict[SandboxMode, str | None] = {
    "default": None,
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "danger-full-access": "danger-full-access",
}


@dataclass(frozen=True)
class CodexNetworkOverride:
    """Deprecated native network override retained for its warning release."""

    value: object
    argument: str | None


@dataclass(frozen=True)
class CodexNetworkSettings:
    """The Codex config values Meridian owns for one launch."""

    network_access: bool | None = None
    proxy_enabled: bool = False
    domains: tuple[tuple[str, Literal["allow", "deny"]], ...] = ()
    override: CodexNetworkOverride | None = None


@dataclass(frozen=True)
class CodexPermissions:
    """Codex's launch-time permission settings; ``None`` leaves Codex's default.

    ``builtin_agents_disabled``: Codex's own multi-agent tools are invisible to Meridian,
    and v2 tells a lone spawn it is `/root` of a team, so it can message itself and wait
    forever.
    """

    sandbox_mode: str | None
    network: CodexNetworkSettings
    approval_policy: str | None
    shell_tool: bool | None
    web_search: bool | None
    view_image: bool | None
    builtin_agents_disabled: bool


def _tool_switch(rule: ToolRule) -> tuple[str, bool, str] | None:
    """Return the Codex switch an exact, unscoped tool rule controls.

    An allowlist's deny has none: the switch would refuse the scoped allows too, and a shell
    allowlist would leave Codex nothing to read files with.
    """

    if (
        rule.pattern is not None
        or rule.action == "ask"
        or rule.origin in {ALLOWLIST_ORIGIN, PLATFORM_ORIGIN}
    ):
        return None
    enabled = rule.action == "allow"
    if rule.capability == "bash":
        value = str(enabled).lower()
        return "shell_tool", enabled, f"-c features.shell_tool={value}"
    if rule.capability in {"web", "web_search"}:
        note = "-c tools.web_search=true" if enabled else '-c web_search="disabled"'
        return "web_search", enabled, note
    if rule.capability == "image":
        verb = "--enable" if enabled else "--disable"
        return "view_image", enabled, f"{verb} view_image"
    return None


def _network_override(native_config: Mapping[str, object]) -> CodexNetworkOverride | None:
    for segments, value in flatten_native_config(native_config):
        key = ".".join(segments)
        if key != CODEX_CONFIG_NETWORK_ACCESS_KEY:
            continue
        literal = codex_toml_literal(value)
        argument = (
            f"{key}={literal}"
            if isinstance(value, bool) and literal is not None and codex_key_addressable(segments)
            else None
        )
        return CodexNetworkOverride(value=value, argument=argument)
    return None


def project_codex_network(
    facts: EffectiveNetworkFacts,
) -> NetworkProjection[CodexNetworkSettings]:
    """Project Codex network config and the access it actually leaves."""

    override = _network_override(facts.native_config)
    if facts.sandbox == "read-only":
        return NetworkProjection(
            native=CodexNetworkSettings(override=override),
            effective=Hosts(),
        )
    if facts.sandbox != "workspace-write":
        return NetworkProjection(
            native=CodexNetworkSettings(override=override),
            effective=Allow(),
        )
    if override is not None and override.argument is not None:
        assert isinstance(override.value, bool)
        return NetworkProjection(
            native=CodexNetworkSettings(override=override),
            effective=Allow() if override.value else Hosts(),
        )

    policy = facts.network
    if isinstance(policy, Allow) and not policy.deny:
        return NetworkProjection(
            native=CodexNetworkSettings(network_access=True, override=override),
            effective=policy,
        )
    if isinstance(policy, Hosts) and not policy.allow:
        return NetworkProjection(
            native=CodexNetworkSettings(network_access=False, override=override),
            effective=policy,
        )

    domains: dict[str, Literal["allow", "deny"]] = {}
    if isinstance(policy, Allow):
        domains["*"] = "allow"
    else:
        domains.update((host, "allow") for host in sorted(policy.allow))
    domains.update((host, "deny") for host in sorted(policy.deny))
    return NetworkProjection(
        native=CodexNetworkSettings(
            network_access=True,
            proxy_enabled=True,
            domains=tuple(domains.items()),
            override=override,
        ),
        effective=policy,
    )


def _compile_codex_permission_values(
    spec: ResolvedLaunchSpec,
) -> tuple[CodexPermissions, NetworkProjection[CodexNetworkSettings]]:
    """Resolve every native permission value, including the deprecated override, once."""

    policy = spec.permission_policy
    # Under an allowlist, search and image viewing are off unless a rule allows them. The
    # shell stays on because Codex reads files through it.
    off = False if policy.is_allowlist else None
    switches: dict[str, bool | None] = {
        "shell_tool": None,
        "web_search": off,
        "view_image": off,
    }
    # `web_search` is narrower than `web`, so it decides the search switch whatever the order.
    for rule in sorted(policy.rules, key=lambda rule: rule.capability == "web_search"):
        switch = _tool_switch(rule)
        if switch is not None:
            switches[switch[0]] = switch[1]

    network = project_codex_network(spec.effective_network_facts())
    return (
        CodexPermissions(
            sandbox_mode=_SANDBOX_MODE_BY_MODE[policy.sandbox],
            network=network.native,
            approval_policy=_APPROVAL_POLICY_BY_MODE[policy.approval],
            shell_tool=switches["shell_tool"],
            web_search=switches["web_search"],
            view_image=switches["view_image"],
            builtin_agents_disabled=any(
                rule.kind == "builtin-agents" for rule in policy.platform_rules
            ),
        ),
        network,
    )


def _codex_report(
    spec: ResolvedLaunchSpec,
    policy: PermissionPolicy,
    permissions: CodexPermissions,
    network: NetworkProjection[CodexNetworkSettings],
    native_config_report: EnforcementReport,
) -> EnforcementReport:
    lines: list[ReportLine] = []
    for rule in policy.rules:
        switch = _tool_switch(rule)
        if (
            rule.origin == PLATFORM_ORIGIN
            and rule.capability == "agent"
            and permissions.builtin_agents_disabled
        ):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "platform",
                    "-c features.multi_agent_v2=false -c agents.max_depth=0",
                )
            )
        elif switch is not None and getattr(permissions, switch[0]) != switch[1]:
            lines.append(ReportLine(rule_element(rule), "partial", "overridden by a narrower rule"))
        elif switch is not None:
            lines.append(ReportLine(rule_element(rule), "native", switch[2]))
        elif rule.pattern is not None and rule.capability in {"bash", "web", "web_search", "image"}:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "unsupported",
                    "Codex's switch cannot express a scoped rule",
                )
            )
        elif rule.action == "ask":
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "unsupported",
                    "Codex has no per-launch tool approval rule",
                )
            )
        elif rule.capability == "*" and rule.action == "deny" and rule.pattern is None:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    "web search and image viewing off unless allowed; the shell stays on, "
                    "since Codex reads files through it",
                )
            )
    for platform in policy.platform_rules:
        if platform.kind == "builtin-agents":
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "-c features.multi_agent_v2=false -c agents.max_depth=0",
                )
            )
        elif platform.kind == "meridian-commands":
            commands = ", ".join(
                f"{command.match} {command.command!r}" for command in platform.commands
            )
            rules_error = spec.codex_runtime.rules_file_error
            rules_path = spec.codex_runtime.rules_file_path
            constrained_sandbox = policy.sandbox in CONSTRAINED_SANDBOX_MODES
            spawn_escape = constrained_sandbox and policy.has_spawn_capability
            if spawn_escape and rules_error is not None:
                note = (
                    "Meridian could not write its Codex rules file, so commands remain subject "
                    f"to the sandbox ({rules_error}); requested matches: {commands}"
                )
            elif spawn_escape and rules_path is None:
                note = (
                    "a real launch would write $CODEX_HOME/rules/meridian.rules so "
                    "`meridian spawn` can run outside Codex's sandbox; other requested "
                    f"matches remain ordinary shell calls ({commands})"
                )
            elif spawn_escape:
                note = (
                    "the Meridian-owned user rules file lets `meridian spawn` run outside "
                    "Codex's sandbox for every Codex session of this user; other requested "
                    f"matches remain ordinary shell calls ({commands})"
                )
            else:
                note = (
                    f"Codex cannot grant shell command matches ({commands}); they run as "
                    "ordinary shell calls, subject to its sandbox and approval policy"
                )
            lines.append(
                ReportLine(
                    platform_element(platform),
                    (
                        "partial"
                        if spawn_escape
                        and permissions.shell_tool is not False
                        and rules_error is None
                        else "unsupported"
                    ),
                    "the shell is disabled, so the commands cannot run"
                    if permissions.shell_tool is False
                    else note,
                )
            )
    if permissions.sandbox_mode is not None:
        sandbox_note = f"sandbox_mode={permissions.sandbox_mode}"
        sandbox_status = "native"
        if policy.sandbox in CONSTRAINED_SANDBOX_MODES and policy.has_spawn_capability:
            rules_error = spec.codex_runtime.rules_file_error
            rules_path = spec.codex_runtime.rules_file_path
            if rules_error is not None:
                sandbox_status = "partial"
                sandbox_note += (
                    "; Meridian could not write "
                    f"{rules_path or '$CODEX_HOME/rules/meridian.rules'}, so `meridian spawn` "
                    f"remains inside Codex's sandbox: {rules_error}"
                )
            elif rules_path is None:
                sandbox_note += (
                    "; a real launch would write $CODEX_HOME/rules/meridian.rules so "
                    "`meridian spawn` can run outside Codex's sandbox"
                )
            else:
                sandbox_note += (
                    "; `meridian spawn` runs outside Codex's sandbox via "
                    f"{rules_path}, for every Codex session of this user"
                )
        lines.append(
            ReportLine(
                f"sandbox: {policy.sandbox}",
                sandbox_status,
                sandbox_note,
            )
        )
    network_element = f"network: {policy.network_mode}"
    network_override = network.native.override
    if (
        policy.sandbox == "workspace-write"
        and network_override is not None
        and network_override.argument is not None
    ):
        override_value = network_override.value
        assert isinstance(override_value, bool)
        status: ReportStatus = "native" if network.effective == policy.network else "partial"
        note = "deprecated native_config sandbox_workspace_write.network_access " + (
            "matches the profile network field"
            if status == "native"
            else "takes precedence over the profile network field for this release"
        )
        lines.append(
            ReportLine(
                network_element,
                status,
                note,
            )
        )
    elif policy.sandbox == "workspace-write":
        if network.native.proxy_enabled:
            domains = codex_toml_literal(dict(network.native.domains))
            lines.append(
                ReportLine(
                    network_element,
                    "partial",
                    "-c sandbox_workspace_write.network_access=true; "
                    "-c features.network_proxy.enabled=true; "
                    f"-c features.network_proxy.domains={domains}; "
                    "Codex still permits local services",
                )
            )
        elif isinstance(network.effective, Allow):
            lines.append(
                ReportLine(
                    network_element,
                    "native",
                    "-c sandbox_workspace_write.network_access=true",
                )
            )
        else:
            rules_error = spec.codex_runtime.rules_file_error
            escaped = rules_error is None
            lines.append(
                ReportLine(
                    network_element,
                    "partial" if escaped else "native",
                    "-c sandbox_workspace_write.network_access=false"
                    + (
                        "; the Meridian-owned user rule still lets `meridian spawn` run "
                        "outside the sandbox with network access"
                        if escaped
                        else ""
                    ),
                )
            )
    elif policy.sandbox == "read-only":
        if isinstance(policy.network, Allow):
            lines.append(
                ReportLine(
                    network_element,
                    "unsupported",
                    "Codex read-only has no network-enabled mode",
                )
            )
        else:
            lines.append(
                ReportLine(
                    network_element,
                    "native",
                    "Codex read-only keeps shell network and sockets off",
                )
            )
    elif policy.sandbox == "danger-full-access":
        enforced = network.effective == policy.network
        lines.append(
            ReportLine(
                network_element,
                "native" if enforced else "unsupported",
                "danger-full-access leaves shell network unrestricted"
                if enforced
                else "not enforced under danger-full-access; shell network remains available",
            )
        )
    else:
        enforced = network.effective == policy.network
        lines.append(
            ReportLine(
                network_element,
                "partial" if enforced else "unsupported",
                "sandbox is left to Codex, so Meridian does not project network_access",
            )
        )
    if permissions.approval_policy is not None:
        note = f"approval_policy={permissions.approval_policy}"
        if policy.approval == "confirm":
            lines.append(
                ReportLine(
                    f"approval: {policy.approval}",
                    "partial",
                    f"{note}; Codex has no stricter mode, so confirm behaves as auto",
                )
            )
        else:
            lines.append(ReportLine(f"approval: {policy.approval}", "native", note))
    lines.extend(native_config_report)
    lines.extend(compaction_report(spec, harness="Codex"))
    return complete_report(policy, lines, harness="Codex")


def compile_codex_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[CodexPermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Codex, with the report of what it did."""

    policy = spec.permission_policy
    permissions, network = _compile_codex_permission_values(spec)
    _, native_config_report = project_codex_native_config(spec.native_config)
    return permissions, _codex_report(
        spec,
        policy,
        permissions,
        network,
        native_config_report,
    )


__all__ = [
    "CodexPermissions",
    "compile_codex_permissions",
    "project_codex_network",
]
