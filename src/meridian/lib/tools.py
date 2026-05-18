"""Shared ToolsField types and normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

type ToolAction = Literal["allow", "deny", "ask"]
type ToolsField = Literal["allow", "deny"] | dict[str, ToolAction]

_TOOL_ACTIONS: frozenset[ToolAction] = frozenset({"allow", "deny", "ask"})
_TOOLS_SHORTHAND_ACTIONS: frozenset[Literal["allow", "deny"]] = frozenset({"allow", "deny"})


def normalize_tool_action(raw: object, *, source: str) -> ToolAction:
    """Normalize one tool action value."""

    normalized = str(raw).strip().lower()
    if normalized in _TOOL_ACTIONS:
        return normalized
    allowed = ", ".join(sorted(_TOOL_ACTIONS))
    raise ValueError(f"Invalid tools action for '{source}': expected one of {allowed}.")


def normalize_tool_key(raw: object, *, source: str) -> str:
    """Normalize one tools map key.

    Keys are case-insensitive on the capability prefix. Scoped pattern payload
    keeps original casing to avoid changing command/path match behavior.
    """

    key = str(raw).strip()
    if not key:
        raise ValueError(f"Invalid tools key for '{source}': key must not be empty.")
    scoped_start = key.find("(")
    if scoped_start <= 0 or not key.endswith(")"):
        return key.lower()
    capability = key[:scoped_start].strip().lower()
    scoped = key[scoped_start:]
    if not capability:
        raise ValueError(f"Invalid tools key for '{source}': key must not be empty.")
    return f"{capability}{scoped}"


def parse_tools_field(raw: object, *, source: str) -> ToolsField | None:
    """Parse the abstract tools field from frontmatter content."""

    if raw is None:
        return None
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in _TOOLS_SHORTHAND_ACTIONS:
            return normalized
        allowed = ", ".join(sorted(_TOOLS_SHORTHAND_ACTIONS))
        raise ValueError(f"Invalid tools value for '{source}': expected one of {allowed}.")
    if isinstance(raw, Mapping):
        normalized_map: dict[str, ToolAction] = {}
        typed_raw = cast("Mapping[object, object]", raw)
        for raw_key, raw_value in typed_raw.items():
            key = normalize_tool_key(raw_key, source=source)
            normalized_map[key] = normalize_tool_action(raw_value, source=f"{source}.{key}")
        return normalized_map
    raise ValueError(
        f"Invalid tools value for '{source}': expected 'allow'/'deny' or a mapping."
    )


def tools_field_to_map(tools: ToolsField | None) -> dict[str, ToolAction]:
    """Return a concrete tools map.

    Shorthand forms become ``{\"*\": \"allow\"}`` / ``{\"*\": \"deny\"}``.
    """

    if tools is None:
        return {}
    if isinstance(tools, str):
        return {"*": tools}
    return dict(tools)


def _split_capability_key(raw_key: str) -> tuple[str, str | None]:
    key = raw_key.strip()
    scoped_start = key.find("(")
    if scoped_start <= 0 or not key.endswith(")"):
        return key.lower(), None
    return key[:scoped_start].strip().lower(), key[scoped_start + 1 : -1]


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
    rules = tools_field_to_map(tools)
    if normalized_capability in rules:
        return rules[normalized_capability]
    for raw_key, action in rules.items():
        key_capability, key_scope = _split_capability_key(raw_key)
        if key_scope is None and key_capability == normalized_capability:
            return action
    return rules.get("*")
