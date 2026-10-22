"""Codex's compile of the permission policy.

Codex takes a sandbox mode and an approval policy per launch. Unscoped allow and deny
rules for shell, web search, and image viewing compile to Codex's per-launch switches;
Codex has no native carrier for the other tool rules. The switches read semantic
capabilities, never harness-native names. The projectors spell this typed result for
``codex exec``, ``codex app-server``, and the thread payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolRule

_APPROVAL_POLICY_BY_MODE: dict[str, str | None] = {
    "default": None,
    "auto": "on-request",
    # Codex 0.163 accepts only ``on-request`` and ``never``; ``untrusted`` is rejected
    # at startup, so ``confirm`` cannot be stricter than ``auto`` here.
    "confirm": "on-request",
    "never": "never",
    "yolo": "never",
}

_SANDBOX_MODE_BY_MODE: dict[str, str | None] = {
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
    approval_policy: str | None
    shell_tool: bool | None
    web_search: bool | None
    view_image: bool | None
    builtin_agents_disabled: bool


def map_codex_approval_policy(approval_mode: str) -> str | None:
    """Map Meridian approval mode to Codex approval policy."""

    if approval_mode not in _APPROVAL_POLICY_BY_MODE:
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested approval mode "
            f"'{approval_mode}' on this CLI/protocol version"
        )
    mapped = _APPROVAL_POLICY_BY_MODE[approval_mode]
    if mapped is None and approval_mode != "default":
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested approval mode "
            f"'{approval_mode}' on this CLI/protocol version"
        )
    return mapped


def map_codex_sandbox_mode(sandbox_mode: str) -> str | None:
    """Map Meridian sandbox mode to Codex sandbox mode."""

    if sandbox_mode not in _SANDBOX_MODE_BY_MODE:
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested sandbox mode "
            f"'{sandbox_mode}' on this CLI/protocol version"
        )
    mapped = _SANDBOX_MODE_BY_MODE[sandbox_mode]
    if mapped is None and sandbox_mode != "default":
        raise HarnessCapabilityMismatch(
            "Codex cannot express requested sandbox mode "
            f"'{sandbox_mode}' on this CLI/protocol version"
        )
    return mapped


def _tool_switch(rule: ToolRule) -> tuple[str, bool, str] | None:
    """Return the Codex switch an exact, unscoped tool rule controls."""

    if rule.pattern is not None or rule.action == "ask":
        return None
    enabled = rule.action == "allow"
    if rule.capability == "bash":
        value = str(enabled).lower()
        return "shell_tool", enabled, f"-c features.shell_tool={value}"
    if rule.capability == "web":
        note = "-c tools.web_search=true" if enabled else '-c web_search="disabled"'
        return "web_search", enabled, note
    if rule.capability == "web_search" and enabled:
        return "web_search", True, "-c tools.web_search=true"
    if rule.capability == "image":
        verb = "--enable" if enabled else "--disable"
        return "view_image", enabled, f"{verb} view_image"
    return None


def _codex_report(policy: PermissionPolicy, permissions: CodexPermissions) -> EnforcementReport:
    lines: list[ReportLine] = []
    for rule in policy.rules:
        switch = _tool_switch(rule)
        if switch is not None:
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
    for platform in policy.platform_rules:
        if platform.kind == "builtin-agents":
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "-c features.multi_agent_v2=false -c agents.max_depth=0",
                )
            )
    if permissions.sandbox_mode is not None:
        lines.append(
            ReportLine(
                f"sandbox: {policy.sandbox}",
                "native",
                f"sandbox_mode={permissions.sandbox_mode}",
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
    return complete_report(policy, lines, harness="Codex")


def compile_codex_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[CodexPermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Codex, with the report of what it did."""

    policy = spec.permission_policy
    switches: dict[str, bool | None] = {
        "shell_tool": None,
        "web_search": None,
        "view_image": None,
    }
    for rule in policy.rules:
        switch = _tool_switch(rule)
        if switch is not None:
            switches[switch[0]] = switch[1]
    permissions = CodexPermissions(
        sandbox_mode=map_codex_sandbox_mode(policy.sandbox),
        approval_policy=map_codex_approval_policy(policy.approval),
        shell_tool=switches["shell_tool"],
        web_search=switches["web_search"],
        view_image=switches["view_image"],
        builtin_agents_disabled=any(
            rule.kind == "builtin-agents" for rule in policy.platform_rules
        ),
    )
    return permissions, _codex_report(policy, permissions)


__all__ = [
    "CodexPermissions",
    "compile_codex_permissions",
    "map_codex_approval_policy",
    "map_codex_sandbox_mode",
]
