"""Permission policy carried on the resolved launch spec.

One transport-neutral value built at bind: the tool rules, the sandbox mode and
the approval mode. Each harness compiles it into its own native projection in
``lib/harness/projections/permissions_<harness>.py``; nothing here knows a
harness.
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


class PermissionPolicy(BaseModel):
    """Tool rules plus the resolved sandbox and approval modes for one launch."""

    model_config = ConfigDict(frozen=True)

    rules: tuple[ToolRule, ...] = ()
    sandbox: SandboxMode = "default"
    approval: ApprovalMode = "default"


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
        sandbox=resolved_sandbox,
        approval=_parse_approval_value(approval),
    )


__all__ = [
    "ApprovalMode",
    "PermissionPolicy",
    "SandboxMode",
    "ToolRule",
    "build_permission_policy",
]
