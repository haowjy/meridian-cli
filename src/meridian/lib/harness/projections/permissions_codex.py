"""Codex's compile of the permission policy.

Codex takes a sandbox mode and an approval policy per launch. Unscoped allow and deny
rules for shell, web search, and image viewing compile to Codex's per-launch switches;
Codex has no native carrier for the other tool rules. The switches read semantic
capabilities, never harness-native names. The projectors spell this typed result for
``codex exec``, ``codex app-server``, and the thread payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_NETWORK_ACCESS_KEY,
    codex_native_config_has_key,
    project_codex_native_config,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    ALLOWLIST_ORIGIN,
    CONSTRAINED_SANDBOX_MODES,
    ApprovalMode,
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
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
class CodexPermissions:
    """Codex's launch-time permission settings; ``None`` leaves Codex's default.

    ``builtin_agents_disabled``: Codex's own multi-agent tools are invisible to Meridian,
    and v2 tells a lone spawn it is `/root` of a team, so it can message itself and wait
    forever.
    """

    sandbox_mode: str | None
    network_access: bool | None
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


def _codex_report(
    spec: ResolvedLaunchSpec,
    policy: PermissionPolicy,
    permissions: CodexPermissions,
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
            constrained_sandbox = policy.sandbox in CONSTRAINED_SANDBOX_MODES
            if constrained_sandbox and rules_error is not None:
                note = (
                    "Meridian could not write its Codex rules file, so commands remain subject "
                    f"to the sandbox ({rules_error}); requested matches: {commands}"
                )
            elif constrained_sandbox:
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
                        if constrained_sandbox
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
        if policy.sandbox in CONSTRAINED_SANDBOX_MODES:
            rules_error = spec.codex_runtime.rules_file_error
            rules_path = spec.codex_runtime.rules_file_path or "$CODEX_HOME/rules/meridian.rules"
            if rules_error is None:
                sandbox_note += (
                    "; `meridian spawn` runs outside Codex's sandbox via "
                    f"{rules_path}, for every Codex session of this user"
                )
            else:
                sandbox_status = "partial"
                sandbox_note += (
                    f"; Meridian could not write {rules_path}, so `meridian spawn` remains "
                    f"inside Codex's sandbox: {rules_error}"
                )
        lines.append(
            ReportLine(
                f"sandbox: {policy.sandbox}",
                sandbox_status,
                sandbox_note,
            )
        )
    network_element = f"network: {policy.network}"
    native_network_override = codex_native_config_has_key(
        spec.native_config, CODEX_CONFIG_NETWORK_ACCESS_KEY
    )
    if policy.sandbox == "workspace-write" and native_network_override:
        lines.append(
            ReportLine(
                network_element,
                "partial",
                "deprecated native_config sandbox_workspace_write.network_access takes "
                "precedence over the profile network field for this release",
            )
        )
    elif policy.sandbox == "workspace-write":
        if policy.network == "allow":
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
                    "Codex's workspace-write default keeps shell network and sockets off"
                    + (
                        "; the Meridian-owned user rule still lets `meridian spawn` run "
                        "outside the sandbox with network access"
                        if escaped
                        else ""
                    ),
                )
            )
    elif policy.sandbox == "read-only":
        if policy.network == "allow" and policy.network_explicit:
            lines.append(
                ReportLine(
                    network_element,
                    "unsupported",
                    "Codex read-only has no network-enabled mode",
                )
            )
        elif policy.network == "allow":
            lines.append(
                ReportLine(
                    network_element,
                    "native",
                    "network was left unset; Codex read-only keeps its native shell "
                    "network-off behavior, with no authored allow request to report as a gap",
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
        lines.append(
            ReportLine(
                network_element,
                "native" if policy.network == "allow" else "unsupported",
                "danger-full-access leaves shell network unrestricted"
                if policy.network == "allow"
                else "not enforced under danger-full-access; shell network remains available",
            )
        )
    else:
        lines.append(
            ReportLine(
                network_element,
                "partial" if policy.network == "allow" else "unsupported",
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
    # Under an allowlist, search and image viewing are off unless a rule allows them. The shell
    # stays on: Codex reads files through it, so turning it off would leave nothing to read with.
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
    permissions = CodexPermissions(
        sandbox_mode=_SANDBOX_MODE_BY_MODE[policy.sandbox],
        network_access=(
            True if policy.sandbox == "workspace-write" and policy.network == "allow" else None
        ),
        approval_policy=_APPROVAL_POLICY_BY_MODE[policy.approval],
        shell_tool=switches["shell_tool"],
        web_search=switches["web_search"],
        view_image=switches["view_image"],
        builtin_agents_disabled=any(
            rule.kind == "builtin-agents" for rule in policy.platform_rules
        ),
    )
    _, native_config_report = project_codex_native_config(spec.native_config)
    return permissions, _codex_report(spec, policy, permissions, native_config_report)


__all__ = ["CodexPermissions", "compile_codex_permissions"]
