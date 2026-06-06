"""Permission-resolution stage ownership for launch composition."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

from meridian.lib.core.types import normalize_mars_target_name
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
from meridian.lib.tools import (
    ToolAction,
    ToolsField,
    normalize_tool_action,
    normalize_tool_key,
    resolve_tool_action,
    tools_field_to_map,
)

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
_CLAUDE_BUILTIN_AGENT_DENY_RULES: tuple[str, ...] = (
    "agent(Explore)",
    "agent(Plan)",
    "agent(General-purpose)",
    "agent(general-purpose)",
)


def _resolve_opencode_override(*, tools: ToolsField | None) -> str | None:
    if tools is None:
        return None
    return compile_tools_to_opencode_permission(tools)


def project_has_claude_agent_copy(project_root: Path) -> bool:
    """Return whether effective Mars config enables Claude native agent copies."""

    targets: tuple[str, ...] = ()
    agent_copy_harnesses: tuple[str, ...] = ()
    for config_name in ("mars.toml", "mars.local.toml"):
        config_path = project_root / config_name
        if not config_path.is_file():
            continue
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
        settings = cast("object", payload.get("settings"))
        if not isinstance(settings, dict):
            continue
        typed_settings = cast("dict[str, object]", settings)
        raw_targets = typed_settings.get("targets")
        if isinstance(raw_targets, list):
            targets = tuple(
                normalize_mars_target_name(str(target))
                for target in cast("list[object]", raw_targets)
            )
        raw_managed_root = typed_settings.get("managed_root")
        if not targets and isinstance(raw_managed_root, str):
            normalized_root = normalize_mars_target_name(raw_managed_root)
            if normalized_root:
                targets = (normalized_root,)
        meridian_table = typed_settings.get("meridian")
        if not isinstance(meridian_table, dict):
            continue
        agent_copy = cast("dict[str, object]", meridian_table).get("agent_copy")
        if not isinstance(agent_copy, dict) or "harnesses" not in agent_copy:
            continue
        typed_agent_copy = cast("dict[str, object]", agent_copy)
        harnesses = typed_agent_copy.get("harnesses")
        if isinstance(harnesses, list):
            agent_copy_harnesses = tuple(
                str(harness).strip().lower()
                for harness in cast("list[object]", harnesses)
            )
    return "claude" in agent_copy_harnesses and "claude" in targets


def _normalized_tools_map(tools: ToolsField | None) -> dict[str, ToolAction]:
    return {
        normalize_tool_key(key, source="tools"): normalize_tool_action(
            action,
            source=f"tools.{key}",
        )
        for key, action in tools_field_to_map(tools).items()
    }


def resolve_claude_native_agent_permission_request(
    *,
    tools: ToolsField | None,
    has_claude_agent_copy: bool,
) -> ToolsField | None:
    """Apply Claude native Agent defaults from the Mars agent-copy boundary."""

    rules = _normalized_tools_map(tools)
    if not has_claude_agent_copy:
        for key in tuple(rules):
            capability, _separator, _scope = key.partition("(")
            if capability == "agent":
                rules[key] = "deny"
        rules["agent"] = "deny"
    for key in _CLAUDE_BUILTIN_AGENT_DENY_RULES:
        rules[key] = "deny"
    if not rules:
        return tools
    return rules


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
    allow_native_agent_tool: bool = False,
) -> ToolsField | None:
    """Apply Meridian's managed-spawn boundary for nested Claude launches."""

    _ = has_profile
    deny_additions = compute_nested_claude_deny_additions(
        profile_tools=profile_tools,
        existing_tools=tools,
    )
    if allow_native_agent_tool:
        deny_additions = tuple(
            capability for capability in deny_additions if capability != "agent"
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
    "project_has_claude_agent_copy",
    "resolve_claude_native_agent_permission_request",
    "resolve_nested_claude_permission_request",
    "resolve_permission_pipeline",
]
