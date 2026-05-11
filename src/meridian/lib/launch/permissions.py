"""Permission-resolution stage ownership for launch composition."""

from __future__ import annotations

from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
    ToolsPermissionResolver,
    UnsafeNoOpPermissionResolver,
    build_permission_config,
    build_permission_resolver,
    compile_tools_to_opencode_permission,
    infer_codex_sandbox_from_tools,
)
from meridian.lib.tools import ToolsField, resolve_tool_action, tools_field_to_map

PermissionResolverImpl = (
    TieredPermissionResolver
    | ToolsPermissionResolver
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


_NESTED_CLAUDE_DELEGATION_CAPABILITIES: frozenset[str] = frozenset({"agent", "task"})


def _resolve_opencode_override(*, tools: ToolsField | None) -> str | None:
    if tools is None:
        return None
    return compile_tools_to_opencode_permission(tools)


def compute_nested_claude_deny_additions(
    *,
    profile_tools: ToolsField | None,
    existing_tools: ToolsField | None,
) -> tuple[str, ...]:
    """Return implicit deny entries for nested Claude managed spawns.

    Excludes tools already denied in `existing_tools` and tools explicitly
    opted out through `profile_tools`.
    """

    opted_out: set[str] = set()
    if profile_tools is not None:
        profile_rules = tools_field_to_map(profile_tools)
        for capability in _NESTED_CLAUDE_DELEGATION_CAPABILITIES:
            action = resolve_tool_action(tools=profile_tools, capability=capability)
            if action in {"allow", "ask"}:
                opted_out.add(capability)
        if "agent" in profile_rules and profile_rules["agent"] == "deny":
            opted_out.discard("agent")
        if "task" in profile_rules and profile_rules["task"] == "deny":
            opted_out.discard("task")

    already_denied = {
        capability
        for capability in _NESTED_CLAUDE_DELEGATION_CAPABILITIES
        if resolve_tool_action(tools=existing_tools, capability=capability) == "deny"
    }
    return tuple(
        capability
        for capability in sorted(_NESTED_CLAUDE_DELEGATION_CAPABILITIES)
        if capability not in opted_out and capability not in already_denied
    )


def _apply_nested_claude_denies(
    *,
    tools: ToolsField | None,
    denied_capabilities: tuple[str, ...],
) -> ToolsField:
    if not denied_capabilities:
        return tools if tools is not None else {"*": "allow"}
    rules = tools_field_to_map(tools)
    if not rules:
        rules["*"] = "allow"
    for capability in denied_capabilities:
        rules[capability] = "deny"
    return rules


def resolve_nested_claude_permission_request(
    *,
    tools: ToolsField | None,
    profile_tools: ToolsField | None,
    has_profile: bool,
) -> ToolsField | None:
    """Apply Meridian's managed-spawn boundary for nested Claude launches."""

    _ = has_profile
    deny_additions = compute_nested_claude_deny_additions(
        profile_tools=profile_tools,
        existing_tools=tools,
    )
    resolved_tools = _apply_nested_claude_denies(tools=tools, denied_capabilities=deny_additions)
    return resolved_tools


def resolve_permission_pipeline(
    *,
    sandbox: str | None,
    tools: ToolsField | None = None,
    approval: str = "default",
    unsafe_no_permissions: bool = False,
) -> tuple[PermissionConfig, PermissionResolverImpl]:
    """Resolve a permission config and concrete resolver for one launch request."""

    if unsafe_no_permissions:
        return PermissionConfig(), UnsafeNoOpPermissionResolver()

    config = build_permission_config(sandbox, approval=approval)
    opencode_override = _resolve_opencode_override(tools=tools)
    if opencode_override is not None:
        config = config.model_copy(update={"opencode_permission_override": opencode_override})
    if config.sandbox == "default":
        inferred = infer_codex_sandbox_from_tools(tools)
        if inferred is not None:
            config = config.model_copy(update={"sandbox": inferred})

    resolver = build_permission_resolver(
        tools=tools,
        permission_config=config,
    )
    return config, resolver


__all__ = [
    "CLAUDE_NATIVE_DELEGATION_TOOLS",
    "PermissionResolverImpl",
    "compute_nested_claude_deny_additions",
    "resolve_nested_claude_permission_request",
    "resolve_permission_pipeline",
]
