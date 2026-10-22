"""Shared tool action and tools-map types."""

from __future__ import annotations

from typing import Literal

type ToolAction = Literal["allow", "deny", "ask"]
type ToolsField = dict[str, ToolAction]


def resolve_tool_action(
    *,
    tools: ToolsField | None,
    capability: str,
) -> ToolAction | None:
    """Resolve one capability action from a ToolsField.

    Exact capability match takes precedence over ``*`` defaults.
    """

    normalized_capability = capability.strip().lower()
    if not normalized_capability:
        return None
    rules = tools or {}
    if normalized_capability in rules:
        return rules[normalized_capability]
    return rules.get("*")
