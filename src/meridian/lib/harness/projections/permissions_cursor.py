"""Cursor's compile of the permission policy: approval and sandbox become two flags."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec


@dataclass(frozen=True)
class CursorPermissions:
    """Cursor's approval flag and ``--sandbox`` value; ``None`` omits the flag."""

    approval: Literal["yolo", "force"] | None
    sandbox: Literal["enabled", "disabled"] | None


def compile_cursor_permissions(spec: ResolvedLaunchSpec) -> CursorPermissions:
    """Compile ``spec.permission_policy`` for Cursor.

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
    return CursorPermissions(approval=approval, sandbox=sandbox)


__all__ = ["CursorPermissions", "compile_cursor_permissions"]
