"""Cursor's compile of the permission policy: approval and sandbox become two flags.

Cursor has no tool rules, so the completed report marks every rule and platform rule
unsupported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.native_config import unsupported_native_config_report
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    UNENFORCED_NETWORK,
    ApprovalMode,
    EffectivePolicy,
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
    SandboxMode,
    complete_report,
    rule_element,
)

type CursorApproval = Literal["yolo", "force"]
type CursorSandbox = Literal["enabled", "disabled"]

_APPROVAL_FLAG: dict[ApprovalMode, CursorApproval | None] = {
    "default": None,
    "confirm": None,
    "auto": "force",
    "never": "yolo",
    "yolo": "yolo",
}
# Cursor's sandbox is binary: ``enabled`` restricts file access to the workspace,
# ``disabled`` removes restrictions. ``workspace-write`` overshoots to ``disabled``; Cursor
# has no finer mode.
_SANDBOX_VALUE: dict[SandboxMode, CursorSandbox | None] = {
    "default": None,
    "read-only": "enabled",
    "workspace-write": "disabled",
    "danger-full-access": "disabled",
}
# What each mode really allows once mapped: ``enabled`` still permits workspace writes.
_EFFECTIVE_SANDBOX: dict[SandboxMode, SandboxMode] = {
    "default": "default",
    "read-only": "workspace-write",
    "workspace-write": "danger-full-access",
    "danger-full-access": "danger-full-access",
}


def effective_cursor_policy(
    policy: PermissionPolicy,
    *,
    native_config: Mapping[str, object],
) -> EffectivePolicy:
    """Return the sandbox Cursor's binary switch gives; its shell network is open."""

    del native_config
    return EffectivePolicy(
        sandbox=_EFFECTIVE_SANDBOX[policy.sandbox], network=UNENFORCED_NETWORK
    )


@dataclass(frozen=True)
class CursorPermissions:
    """Cursor's approval flag and ``--sandbox`` value; ``None`` omits the flag."""

    approval: CursorApproval | None
    sandbox: CursorSandbox | None


def _cursor_report(
    spec: ResolvedLaunchSpec, policy: PermissionPolicy, permissions: CursorPermissions
) -> EnforcementReport:
    unsupported_note = "Cursor does not enforce tool rules or restrictive tool profiles"
    lines = [
        ReportLine(rule_element(rule), "unsupported", unsupported_note) for rule in policy.rules
    ]
    if permissions.sandbox is not None:
        note = f"--sandbox {permissions.sandbox}"
        effective = _EFFECTIVE_SANDBOX[policy.sandbox]
        if effective == policy.sandbox:
            lines.append(ReportLine(f"sandbox: {policy.sandbox}", "native", note))
        else:
            lines.append(
                ReportLine(
                    f"sandbox: {policy.sandbox}",
                    "partial",
                    f"{note}; Cursor's sandbox is on or off, so this runs as {effective}",
                )
            )
    if permissions.approval is not None:
        lines.append(
            ReportLine(f"approval: {policy.approval}", "native", f"--{permissions.approval}")
        )
    elif policy.approval == "confirm":
        lines.append(
            ReportLine(
                f"approval: {policy.approval}",
                "unsupported",
                "Cursor has no confirm switch; no flag passed",
            )
        )
    lines.extend(unsupported_native_config_report(spec.native_config, harness="Cursor"))
    lines.extend(compaction_report(spec, harness="Cursor"))
    return complete_report(policy, lines, harness="Cursor")


def compile_cursor_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[CursorPermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Cursor, with the report of what it did."""

    policy = spec.permission_policy
    permissions = CursorPermissions(
        approval=_APPROVAL_FLAG[policy.approval], sandbox=_SANDBOX_VALUE[policy.sandbox]
    )
    return permissions, _cursor_report(spec, policy, permissions)


__all__ = [
    "CursorPermissions",
    "compile_cursor_permissions",
    "effective_cursor_policy",
]
