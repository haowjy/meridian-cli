"""Permission policy carried on the resolved launch spec.

One transport-neutral value built at bind: the tool rules, the platform rules Meridian
adds, the sandbox mode and the approval mode. Each harness compiles it into its own native
projection in ``lib/harness/projections/permissions_<harness>.py``, which also names the
platform rules natively; nothing here knows a harness.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.tools import ToolAction, ToolsField, resolve_tool_action, tools_field_to_map

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
    {"default", "read-only", "workspace-write", "danger-full-access"}
)
_APPROVAL_MODES: frozenset[ApprovalMode] = frozenset(
    {"default", "auto", "never", "yolo", "confirm"}
)
_ASK_AS_ALLOW: frozenset[ToolAction] = frozenset({"allow", "ask"})


class ToolRule(BaseModel):
    """One tool rule: what it covers, what it does, and how its harness names it.

    ``capability`` and ``pattern`` carry meaning; ``native`` carries emission. Until the
    Mars bundle ships typed rules, ``capability`` is the lowercased head of the bundle's
    native key and ``native`` is that key verbatim, so the two are not independent yet.
    ``native_status`` and ``origin`` mirror the fields of Mars's typed rule.
    """

    model_config = ConfigDict(frozen=True)

    capability: str | None = None
    pattern: str | None = None
    action: ToolAction
    native: tuple[str, ...] = ()
    native_status: Literal["known", "absent", "unknown"] = "known"
    origin: str | None = None


type PlatformRuleKind = Literal["native-subagents", "builtin-agents"]


class PlatformRule(BaseModel):
    """A restriction Meridian puts on a launch whatever the profile says.

    ``native-subagents`` denies the harness's own sub-agent tools so delegation goes
    through Meridian; ``capabilities`` lists the ones to deny (``agent``, ``task``), already
    net of profile opt-outs. ``builtin-agents`` denies the harness's built-in agents.
    A harness that has no native name for a rule leaves it out of its projection.
    """

    model_config = ConfigDict(frozen=True)

    kind: PlatformRuleKind
    capabilities: tuple[str, ...] = ()


class PermissionPolicy(BaseModel):
    """Everything permission-shaped for one launch, before any harness reads it.

    ``native_agent_copy``: Mars installed native agent definitions for the harness, so
    its own agent tool is the project's delegation mechanism and stays usable.
    ``web_search``: the bundle granted the native ``web_search`` tool, which Codex needs
    switched on explicitly (a native key until the typed rules arrive).
    """

    model_config = ConfigDict(frozen=True)

    rules: tuple[ToolRule, ...] = ()
    platform_rules: tuple[PlatformRule, ...] = ()
    sandbox: SandboxMode = "default"
    approval: ApprovalMode = "default"
    native_agent_copy: bool = False
    web_search: bool = False


def _split_key(raw_key: str) -> tuple[str, str | None]:
    key = raw_key.strip()
    scoped_start = key.find("(")
    if scoped_start <= 0 or not key.endswith(")"):
        return key.lower(), None
    return key[:scoped_start].strip().lower(), key[scoped_start + 1 : -1]


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


def _infer_sandbox_from_tools(tools: ToolsField | None) -> SandboxMode | None:
    """Infer a sandbox mode from the tools map when none was configured.

    Written for Codex, applied to every harness. Mars hands over native names
    (``exec_command``, ``apply_patch``) that this looks up as ``bash``/``edit``, so on
    Codex it lands on ``read-only`` whenever ``tools`` is set. Deleted with the Codex
    commit (D8: an unset sandbox then means the harness default).
    """

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


def build_permission_policy(
    *,
    tools: ToolsField | None,
    sandbox: str | None,
    approval: str = "default",
    platform_rules: tuple[PlatformRule, ...] = (),
    native_agent_copy: bool = False,
    web_search: bool = False,
) -> PermissionPolicy:
    """Build the policy from the resolved tools map and execution policy.

    ``sandbox`` and ``approval`` are the already-resolved execution policy values; this
    validates them and never re-resolves precedence.
    """

    resolved_sandbox = _parse_sandbox_value(sandbox)
    if resolved_sandbox == "default":
        resolved_sandbox = _infer_sandbox_from_tools(tools) or resolved_sandbox
    rules: list[ToolRule] = []
    for raw_key, action in tools_field_to_map(tools).items():
        capability, pattern = _split_key(raw_key)
        rules.append(
            ToolRule(capability=capability, pattern=pattern, action=action, native=(raw_key,))
        )
    return PermissionPolicy(
        rules=tuple(rules),
        platform_rules=platform_rules,
        sandbox=resolved_sandbox,
        approval=_parse_approval_value(approval),
        native_agent_copy=native_agent_copy,
        web_search=web_search,
    )


__all__ = [
    "ApprovalMode",
    "PermissionPolicy",
    "PlatformRule",
    "SandboxMode",
    "ToolRule",
    "build_permission_policy",
]
