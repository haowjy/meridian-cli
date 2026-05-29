"""Permission configuration and resolver construction."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.tools import ToolAction, ToolsField, resolve_tool_action, tools_field_to_map

logger = logging.getLogger(__name__)


type SandboxMode = Literal[
    "default",
    "read-only",
    "workspace-write",
    "danger-full-access",
]


type ApprovalMode = Literal[
    "default",
    "auto",
    "never",
    "yolo",
    "confirm",
]


_SANDBOX_MODES: frozenset[SandboxMode] = frozenset(
    {
        "default",
        "read-only",
        "workspace-write",
        "danger-full-access",
    }
)

_APPROVAL_MODES: frozenset[ApprovalMode] = frozenset(
    {
        "default",
        "auto",
        "never",
        "yolo",
        "confirm",
    }
)


class PermissionConfig(BaseModel):
    """Transport-neutral permission intent."""

    model_config = ConfigDict(frozen=True)

    sandbox: SandboxMode = "default"
    approval: ApprovalMode = "default"
    # Optional OpenCode permission map JSON derived from ToolsField.
    opencode_permission_override: str | None = None


def _install_readonly_permission_field(field_name: str) -> None:
    def _get(instance: PermissionConfig) -> object:
        return instance.__dict__[field_name]

    def _set(instance: PermissionConfig, value: object) -> None:
        _ = instance, value
        raise TypeError(f"PermissionConfig is frozen; cannot mutate '{field_name}'.")

    setattr(PermissionConfig, field_name, property(_get, _set))


for _field_name in (
    "sandbox",
    "approval",
    "opencode_permission_override",
):
    _install_readonly_permission_field(_field_name)


class UnsafeNoOpPermissionResolver(BaseModel):
    """Explicit unsafe opt-out resolver with no permission enforcement."""

    model_config = ConfigDict(frozen=True)

    def __init__(self, *, _suppress_warning: bool = False, **data: object) -> None:
        super().__init__(**data)
        if not _suppress_warning:
            logger.warning(
                "UnsafeNoOpPermissionResolver constructed; "
                "no permission enforcement will be applied"
            )

    @property
    def config(self) -> PermissionConfig:
        return PermissionConfig()

    def resolve_flags(self) -> tuple[str, ...]:
        return ()


def _parse_approval_value(raw: str) -> ApprovalMode:
    normalized = raw.strip().lower()
    if normalized in _APPROVAL_MODES:
        return normalized
    allowed = ", ".join(sorted(_APPROVAL_MODES))
    raise ValueError(f"Unsupported approval mode '{raw}'. Expected: {allowed}.")


def _parse_sandbox_value(raw: str | None) -> SandboxMode:
    normalized = raw.strip().lower() if raw else "default"
    if normalized in _SANDBOX_MODES:
        return normalized
    allowed = ", ".join(sorted(_SANDBOX_MODES))
    raise ValueError(f"Unsupported sandbox mode '{raw}'. Expected: {allowed}.")


def build_permission_config(
    sandbox: str | None,
    *,
    approval: str = "default",
) -> PermissionConfig:
    """Build and validate a permission configuration."""

    return PermissionConfig(
        sandbox=_parse_sandbox_value(sandbox),
        approval=_parse_approval_value(approval),
    )


type ClaudeToolList = tuple[str, ...]

_ASK_AS_ALLOW: frozenset[ToolAction] = frozenset({"allow", "ask"})

_CAPABILITY_TO_CLAUDE_TOOLS: dict[str, ClaudeToolList] = {
    "bash": ("Bash",),
    "edit": ("Edit", "Write"),
    "write": ("Write",),
    "read": ("Read",),
    "glob": ("Glob",),
    "grep": ("Grep",),
    "list": ("LS",),
    "task": (
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
    ),
    "web": ("WebSearch", "WebFetch"),
    "agent": ("Agent",),
    "external_directory": (),
    "mcp": (),
}


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        normalized = raw.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def _split_key(raw_key: str) -> tuple[str, str | None]:
    key = raw_key.strip()
    scoped_start = key.find("(")
    if scoped_start <= 0 or not key.endswith(")"):
        return key.lower(), None
    return key[:scoped_start].strip().lower(), key[scoped_start + 1 : -1]


def _claude_key_for_capability(capability: str) -> ClaudeToolList:
    mapped = _CAPABILITY_TO_CLAUDE_TOOLS.get(capability.lower())
    if mapped is not None:
        return mapped
    if not capability:
        return ()
    return (capability[:1].upper() + capability[1:],)


def _expand_web_opencode_key(key: str) -> tuple[str, ...]:
    capability, scope = _split_key(key)
    if capability != "web":
        return (key,)
    if scope is None:
        return ("websearch", "webfetch")
    return (f"websearch({scope})", f"webfetch({scope})")


def compile_tools_to_opencode_permission(tools: ToolsField) -> str:
    """Compile abstract ToolsField to OPENCODE_PERMISSION JSON."""

    rules = tools_field_to_map(tools)
    permissions: dict[str, str] = {}
    for raw_key, action in rules.items():
        for key in _expand_web_opencode_key(raw_key):
            permissions[key] = action
    return json.dumps(permissions, sort_keys=True, separators=(",", ":"))


def compile_tools_to_claude_flags(tools: ToolsField | None) -> tuple[str, ...]:
    """Compile abstract ToolsField to Claude tool flags."""

    if tools is None:
        return ()

    rules = tools_field_to_map(tools)
    default_action = rules.get("*")
    allowed: list[str] = []
    disallowed: list[str] = []
    known_all_tools = _dedupe(
        tool for tools_ in _CAPABILITY_TO_CLAUDE_TOOLS.values() for tool in tools_
    )

    for raw_key, action in rules.items():
        if raw_key == "*":
            continue
        capability, scope = _split_key(raw_key)
        mapped = _claude_key_for_capability(capability)
        if action in _ASK_AS_ALLOW:
            if scope is None:
                allowed.extend(mapped)
            else:
                allowed.extend(f"{tool}({scope})" for tool in mapped)
        elif action == "deny" and default_action != "deny":
            if scope is None:
                disallowed.extend(mapped)
            else:
                disallowed.extend(f"{tool}({scope})" for tool in mapped)

    if default_action == "deny" and not allowed:
        # Best-effort representation for "deny everything".
        disallowed.extend(known_all_tools)

    deduped_allowed = _dedupe(allowed)
    deduped_disallowed = _dedupe(disallowed)
    flags: list[str] = []
    if deduped_allowed:
        flags.extend(("--allowedTools", ",".join(deduped_allowed)))
    if deduped_disallowed:
        flags.extend(("--disallowedTools", ",".join(deduped_disallowed)))
    return tuple(flags)


def infer_codex_sandbox_from_tools(tools: ToolsField | None) -> SandboxMode | None:
    """Infer Codex sandbox mode from the tools capability map."""

    if tools is None:
        return None

    def is_allowish(capability: str) -> bool:
        return resolve_tool_action(tools=tools, capability=capability) in _ASK_AS_ALLOW

    if is_allowish("external_directory"):
        return "danger-full-access"

    wildcard = resolve_tool_action(tools=tools, capability="*")
    if wildcard in _ASK_AS_ALLOW and not is_allowish("edit") and not is_allowish("bash"):
        # Explicitly denying both write-capable tool classes from an allow-all baseline.
        return "read-only"

    if wildcard in _ASK_AS_ALLOW and is_allowish("edit") and is_allowish("bash"):
        return "danger-full-access"
    if is_allowish("edit") or is_allowish("bash"):
        return "workspace-write"
    return "read-only"


class TieredPermissionResolver(BaseModel):
    """PermissionResolver implementation backed by one permission config."""

    model_config = ConfigDict(frozen=True)

    config: PermissionConfig

    def resolve_flags(self) -> tuple[str, ...]:
        return ()


class ToolsPermissionResolver(BaseModel):
    """PermissionResolver backed by one abstract ToolsField map."""

    model_config = ConfigDict(frozen=True)

    tools: ToolsField
    fallback_config: PermissionConfig

    @property
    def config(self) -> PermissionConfig:
        return self.fallback_config

    def opencode_permission_json(self) -> str:
        return compile_tools_to_opencode_permission(self.tools)

    def resolve_flags(self) -> tuple[str, ...]:
        return compile_tools_to_claude_flags(self.tools)


def build_permission_resolver(
    *,
    tools: ToolsField | None,
    permission_config: PermissionConfig,
) -> TieredPermissionResolver | ToolsPermissionResolver:
    """Pick the right resolver: explicit tools if specified, else config-based."""
    if tools is not None:
        return ToolsPermissionResolver(
            tools=tools,
            fallback_config=permission_config,
        )
    return TieredPermissionResolver(config=permission_config)


def resolve_permission_pipeline(
    *,
    sandbox: str | None,
    tools: ToolsField | None = None,
    approval: str = "default",
    unsafe_no_permissions: bool = False,
) -> tuple[
    PermissionConfig,
    (
        TieredPermissionResolver
        | ToolsPermissionResolver
        | UnsafeNoOpPermissionResolver
    ),
]:
    """Compatibility wrapper for the stage-owned launch permission resolver."""

    from meridian.lib.launch.permissions import resolve_permission_pipeline as _resolve

    return _resolve(
        sandbox=sandbox,
        tools=tools,
        approval=approval,
        unsafe_no_permissions=unsafe_no_permissions,
    )
