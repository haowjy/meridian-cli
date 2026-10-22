"""OpenCode's compile of the permission policy.

Tool rules become OpenCode's V1 permission map (``{capability-or-scoped-key: action}``).
The V1 launch env carries it as JSON; the V2 connection translates the same map to V2
rules (``project_opencode_streaming.py``). Primaries return ``None``: the OpenCode TUI
handles their permissions natively.
"""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import ToolRule
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


def compile_opencode_permissions(spec: ResolvedLaunchSpec) -> OpenCodePermissions | None:
    """Compile ``spec.permission_policy`` for OpenCode; ``None`` when no rules apply."""

    rules = spec.permission_policy.rules
    if not rules or spec.interactive:
        return None
    permissions: OpenCodePermissions = {}
    for rule in rules:
        for key in _opencode_keys(rule):
            permissions[key] = rule.action
    return dict(sorted(permissions.items()))


__all__ = ["OpenCodePermissions", "compile_opencode_permissions"]
