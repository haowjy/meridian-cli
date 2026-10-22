"""OpenCode's compile of the permission policy.

Tool rules become OpenCode's V1 permission map (``{tool-or-scoped-key: action}``): each rule
contributes the native keys Mars resolved for OpenCode, and the ``*`` rule contributes the
``*`` key. The V1 launch env carries the map as JSON; the V2 connection translates the same
map to V2 rules (``project_opencode_streaming.py``). Primaries return ``None``: the OpenCode
TUI handles their permissions natively. Only tool rules compile; sandbox, approval and
platform rules do not reach OpenCode, and the completed report says so.
"""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import EnforcementReport, ReportLine, complete_report, rule_element
from meridian.lib.tools import ToolAction, ToolRule

type OpenCodePermissions = dict[str, ToolAction]

_STRICTNESS: dict[ToolAction, int] = {"allow": 0, "ask": 1, "deny": 2}


def _opencode_keys(rule: ToolRule) -> tuple[str, ...]:
    """The permission keys a rule sets: its native names, or ``*`` for the default rule."""

    if rule.is_wildcard:
        return ("*",)
    return rule.native if rule.native_status == "known" else ()


def compile_opencode_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[OpenCodePermissions | None, EnforcementReport]:
    """Compile ``spec.permission_policy`` for OpenCode, with the report of what it did.

    The permission map is ``None`` when no rules apply. Rules Mars could not name for
    OpenCode, or that OpenCode lacks, set no key and are left to the completed report.
    Two rules can name the same OpenCode tool (``write`` and ``edit`` both resolve to
    ``edit``); the stricter action (deny over ask over allow) wins, as in the V2 translation.
    """

    policy = spec.permission_policy
    rules = [(rule, _opencode_keys(rule)) for rule in policy.rules]
    permissions: OpenCodePermissions = {}
    if not spec.interactive:
        for rule, keys in rules:
            for key in keys:
                current = permissions.get(key)
                if current is None or _STRICTNESS[rule.action] > _STRICTNESS[current]:
                    permissions[key] = rule.action
    lines: list[ReportLine] = []
    for rule, keys in rules:
        if not keys:
            continue
        element = rule_element(rule)
        if spec.interactive:
            lines.append(
                ReportLine(
                    element,
                    "unsupported",
                    "primary sessions use OpenCode's own permissions; profile rules are not sent",
                )
            )
            continue
        note = f"permission {', '.join(keys)}: {rule.action}"
        beaten = [key for key in keys if permissions[key] != rule.action]
        if beaten:
            lines.append(
                ReportLine(
                    element,
                    "partial",
                    f"{note}; a stricter rule sets {', '.join(beaten)} to "
                    + ", ".join(permissions[key] for key in beaten),
                )
            )
        else:
            lines.append(ReportLine(element, "native", note))
    return (
        (dict(sorted(permissions.items())) or None),
        complete_report(policy, lines, harness="OpenCode"),
    )


__all__ = ["OpenCodePermissions", "compile_opencode_permissions"]
