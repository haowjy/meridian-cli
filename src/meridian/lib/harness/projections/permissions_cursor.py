"""Cursor's compile of the permission policy: approval and sandbox become two flags.

Cursor has no tool rules, so the report marks every rule and platform rule unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    PermissionPolicy,
    ReportLine,
    platform_element,
    rule_element,
)


@dataclass(frozen=True)
class CursorPermissions:
    """Cursor's approval flag and ``--sandbox`` value; ``None`` omits the flag."""

    approval: Literal["yolo", "force"] | None
    sandbox: Literal["enabled", "disabled"] | None


def _cursor_report(policy: PermissionPolicy, permissions: CursorPermissions) -> EnforcementReport:
    lines = [
        ReportLine(rule_element(rule), "unsupported", "Cursor has no tool rules")
        for rule in policy.rules
    ]
    lines.extend(
        ReportLine(platform_element(platform), "unsupported", "not projected for Cursor")
        for platform in policy.platform_rules
    )
    if permissions.sandbox is not None:
        note = f"--sandbox {permissions.sandbox}"
        if policy.sandbox == "workspace-write":
            lines.append(
                ReportLine(
                    f"sandbox: {policy.sandbox}",
                    "partial",
                    f"{note}; Cursor's sandbox is on or off, so workspace-write removes the "
                    "restrictions",
                )
            )
        else:
            lines.append(ReportLine(f"sandbox: {policy.sandbox}", "native", note))
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
    return tuple(lines)


def compile_cursor_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[CursorPermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Cursor, with the report of what it did.

    Cursor's sandbox is binary: ``enabled`` restricts file access to the workspace,
    ``disabled`` removes restrictions. ``workspace-write`` and ``danger-full-access`` both
    map to ``disabled`` (an overshoot for ``workspace-write``; Cursor has no finer mode).
    """

    policy = spec.permission_policy
    approval: Literal["yolo", "force"] | None
    if policy.approval in ("yolo", "never"):
        approval = "yolo"
    elif policy.approval == "auto":
        approval = "force"
    elif policy.approval in ("default", "confirm"):
        approval = None
    else:
        raise HarnessCapabilityMismatch(
            f"Cursor projection does not support approval mode '{policy.approval}'."
        )

    sandbox: Literal["enabled", "disabled"] | None
    if policy.sandbox == "default":
        sandbox = None
    elif policy.sandbox == "read-only":
        sandbox = "enabled"
    elif policy.sandbox in ("workspace-write", "danger-full-access"):
        sandbox = "disabled"
    else:
        raise HarnessCapabilityMismatch(
            f"Cursor projection does not support sandbox mode '{policy.sandbox}'."
        )
    permissions = CursorPermissions(approval=approval, sandbox=sandbox)
    return permissions, _cursor_report(policy, permissions)


__all__ = ["CursorPermissions", "compile_cursor_permissions"]
