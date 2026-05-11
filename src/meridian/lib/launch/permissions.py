"""Permission-resolution stage ownership for launch composition."""

from __future__ import annotations

from meridian.lib.safety.permissions import (
    CombinedToolsResolver,
    DisallowedToolsResolver,
    ExplicitToolsResolver,
    PermissionConfig,
    TieredPermissionResolver,
    UnsafeNoOpPermissionResolver,
    build_permission_config,
    build_permission_resolver,
    opencode_permission_json_for_allowed_tools,
    opencode_permission_json_for_disallowed_tools,
)

PermissionResolverImpl = (
    TieredPermissionResolver
    | ExplicitToolsResolver
    | DisallowedToolsResolver
    | CombinedToolsResolver
    | UnsafeNoOpPermissionResolver
)

CLAUDE_NATIVE_DELEGATION_TOOLS: frozenset[str] = frozenset(
    {
        "Agent",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    }
)
"""Native Claude Code delegation tools denied by default in managed spawns.

These tools let a Claude agent spin up sub-agents outside Meridian's tracking
and policy enforcement. Profiles can opt out per tool via `tools:`.
"""


def _resolve_opencode_override(
    *,
    allowed_tools: tuple[str, ...],
    disallowed_tools: tuple[str, ...],
) -> str | None:
    if allowed_tools:
        return opencode_permission_json_for_allowed_tools(allowed_tools)
    if disallowed_tools:
        return opencode_permission_json_for_disallowed_tools(disallowed_tools)
    return None


def _normalize_tool_name(raw: str) -> str:
    """Normalize a tool name: strip Claude-style qualifiers and lowercase."""
    return raw.split("(", 1)[0].strip().lower()


def compute_nested_claude_deny_additions(
    *,
    profile_allowed_tools: tuple[str, ...],
    existing_disallowed_tools: tuple[str, ...],
) -> tuple[str, ...]:
    """Return implicit deny entries for nested Claude managed spawns.

    Excludes tools already present in `existing_disallowed_tools` and tools
    explicitly opted out through `profile_allowed_tools`.
    """

    opted_out = {_normalize_tool_name(tool) for tool in profile_allowed_tools}
    already_denied = {_normalize_tool_name(tool) for tool in existing_disallowed_tools}
    return tuple(
        tool
        for tool in sorted(CLAUDE_NATIVE_DELEGATION_TOOLS)
        if _normalize_tool_name(tool) not in opted_out
        and _normalize_tool_name(tool) not in already_denied
    )


def remove_disallowed_tools_from_allowlist(
    *,
    allowed_tools: tuple[str, ...],
    disallowed_tools: tuple[str, ...],
) -> tuple[str, ...]:
    """Return allowed tools after explicit denies take precedence."""

    denied = {_normalize_tool_name(tool) for tool in disallowed_tools}
    return tuple(tool for tool in allowed_tools if _normalize_tool_name(tool) not in denied)


def resolve_nested_claude_permission_request(
    *,
    allowed_tools: tuple[str, ...],
    disallowed_tools: tuple[str, ...],
    profile_allowed_tools: tuple[str, ...],
    has_profile: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Apply Meridian's managed-spawn boundary for nested Claude launches."""

    deny_additions = compute_nested_claude_deny_additions(
        profile_allowed_tools=profile_allowed_tools,
        existing_disallowed_tools=disallowed_tools,
    )
    resolved_disallowed_tools = (*disallowed_tools, *deny_additions)
    resolved_allowed_tools = allowed_tools
    if not has_profile:
        resolved_allowed_tools = remove_disallowed_tools_from_allowlist(
            allowed_tools=allowed_tools,
            disallowed_tools=resolved_disallowed_tools,
        )
    return resolved_allowed_tools, resolved_disallowed_tools


def resolve_permission_pipeline(
    *,
    sandbox: str | None,
    allowed_tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
    approval: str = "default",
    unsafe_no_permissions: bool = False,
) -> tuple[PermissionConfig, PermissionResolverImpl]:
    """Resolve a permission config and concrete resolver for one launch request."""

    if unsafe_no_permissions:
        return PermissionConfig(), UnsafeNoOpPermissionResolver()

    config = build_permission_config(sandbox, approval=approval)
    opencode_override = _resolve_opencode_override(
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    if opencode_override is not None:
        config = config.model_copy(update={"opencode_permission_override": opencode_override})

    resolver = build_permission_resolver(
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_config=config,
    )
    return config, resolver


__all__ = [
    "CLAUDE_NATIVE_DELEGATION_TOOLS",
    "PermissionResolverImpl",
    "compute_nested_claude_deny_additions",
    "remove_disallowed_tools_from_allowlist",
    "resolve_nested_claude_permission_request",
    "resolve_permission_pipeline",
]
