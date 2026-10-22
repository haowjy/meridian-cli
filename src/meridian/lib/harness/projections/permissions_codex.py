"""Codex's compile of the permission policy.

Codex takes a sandbox mode and an approval policy per launch. Its tool rules have no
native carrier, so only the two modes compile, plus two switches: the native ``web_search``
tool and Codex's own multi-agent tools. The switches are keyed on a rule's ``capability``
(a mechanism map, not a name table). The projectors (`codex exec` flags, `codex app-server`
flags, the thread payload) spell the typed result three ways.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
    SandboxMode,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolRule, capability_action

_WEB_SEARCH_CAPABILITIES = frozenset({"web", "web_search"})

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
    web_search: bool
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


def _grants_web_search(rule: ToolRule) -> bool:
    """Codex exposes ``web_search`` only when switched on, so a grant alone is inert.

    A rule from a pre-rules snapshot has no capability, only Codex's own native name.
    """

    if rule.action == "deny":
        return False
    if rule.is_legacy:
        return rule.native == ("web_search",)
    return rule.capability in _WEB_SEARCH_CAPABILITIES


def _infer_sandbox(rules: tuple[ToolRule, ...]) -> SandboxMode | None:
    """Infer a sandbox from the tool rules when none was configured.

    Deleted with the Codex commit (an unset sandbox then means Codex's default).
    """

    if not rules:
        return None

    def is_allowish(capability: str) -> bool:
        return capability_action(rules, capability) in {"allow", "ask"}

    if is_allowish("external_directory"):
        return "danger-full-access"
    wildcard = capability_action(rules, "*")
    if wildcard in {"allow", "ask"} and not is_allowish("edit") and not is_allowish("bash"):
        # Explicitly denying both write-capable tool classes from an allow-all baseline.
        return "read-only"
    if wildcard in {"allow", "ask"} and is_allowish("edit") and is_allowish("bash"):
        return "danger-full-access"
    if is_allowish("edit") or is_allowish("bash"):
        return "workspace-write"
    return "read-only"


def _codex_report(
    policy: PermissionPolicy, permissions: CodexPermissions, inferred_sandbox: SandboxMode | None
) -> EnforcementReport:
    lines: list[ReportLine] = []
    for rule in policy.rules:
        if _grants_web_search(rule):
            lines.append(ReportLine(rule_element(rule), "native", "-c tools.web_search=true"))
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
        note = f"sandbox_mode={permissions.sandbox_mode}"
        if inferred_sandbox is not None:
            note += "; inferred from the tool rules because no sandbox was configured"
        lines.append(ReportLine(f"sandbox: {inferred_sandbox or policy.sandbox}", "native", note))
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
    inferred_sandbox = _infer_sandbox(policy.rules) if policy.sandbox == "default" else None
    permissions = CodexPermissions(
        sandbox_mode=map_codex_sandbox_mode(inferred_sandbox or policy.sandbox),
        approval_policy=map_codex_approval_policy(policy.approval),
        web_search=any(_grants_web_search(rule) for rule in policy.rules),
        builtin_agents_disabled=any(
            rule.kind == "builtin-agents" for rule in policy.platform_rules
        ),
    )
    return permissions, _codex_report(policy, permissions, inferred_sandbox)


__all__ = [
    "CodexPermissions",
    "compile_codex_permissions",
    "map_codex_approval_policy",
    "map_codex_sandbox_mode",
]
