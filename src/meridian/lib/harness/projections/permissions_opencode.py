"""OpenCode's compile of the permission policy.

Tool rules become OpenCode's V1 permission map (``{capability-or-scoped-key: action}``).
The V1 launch env carries it as JSON; the V2 connection translates the same map to V2
rules (``project_opencode_streaming.py``). Primaries return ``None``: the OpenCode TUI
handles their permissions natively. Only tool rules compile; sandbox, approval and platform
rules do not reach OpenCode, and the report says so.
"""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    ReportLine,
    ToolRule,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction

type OpenCodePermissions = dict[str, ToolAction]


def _opencode_keys(rule: ToolRule) -> tuple[str, ...]:
    """Expand ``web`` to OpenCode's two web tools; every other key passes through."""

    (key,) = rule.native
    if rule.capability != "web":
        return (key,)
    if rule.pattern is None:
        return ("websearch", "webfetch")
    return (f"websearch({rule.pattern})", f"webfetch({rule.pattern})")


def compile_opencode_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[OpenCodePermissions | None, EnforcementReport]:
    """Compile ``spec.permission_policy`` for OpenCode, with the report of what it did.

    The permission map is ``None`` when no rules apply.
    """

    policy = spec.permission_policy
    lines: list[ReportLine] = []
    permissions: OpenCodePermissions = {}
    for rule in policy.rules:
        keys = _opencode_keys(rule)
        if spec.interactive:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "unsupported",
                    "primary sessions use OpenCode's own permissions; profile rules are not sent",
                )
            )
            continue
        for key in keys:
            permissions[key] = rule.action
        lines.append(
            ReportLine(rule_element(rule), "native", f"permission {', '.join(keys)}: {rule.action}")
        )
    lines.extend(
        ReportLine(platform_element(platform), "unsupported", "not projected for OpenCode")
        for platform in policy.platform_rules
    )
    if policy.sandbox != "default":
        lines.append(
            ReportLine(f"sandbox: {policy.sandbox}", "unsupported", "OpenCode has no sandbox")
        )
    if policy.approval != "default":
        lines.append(
            ReportLine(f"approval: {policy.approval}", "unsupported", "not projected for OpenCode")
        )
    return (dict(sorted(permissions.items())) or None), tuple(lines)


__all__ = ["OpenCodePermissions", "compile_opencode_permissions"]
