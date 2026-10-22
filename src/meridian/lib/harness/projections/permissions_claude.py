"""Claude's compile of the permission policy.

Tool rules become ``--allowedTools`` / ``--disallowedTools`` entries and the approval
mode becomes Claude's permission mode. ``project_claude.py`` turns the typed result into
argv; nothing here builds argv.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.safety.policy import ToolRule

# Claude's tool names per capability. Unknown capabilities are re-capitalized, which
# misnames multi-word tools; the Mars-typed rules replace this table.
_CAPABILITY_TO_CLAUDE_TOOLS: dict[str, tuple[str, ...]] = {
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


@dataclass(frozen=True)
class ClaudePermissions:
    """What Claude is told about permissions for one launch."""

    skip_permission_prompts: bool = False
    permission_mode: Literal["acceptEdits", "default"] | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()


def _claude_tools_for_capability(capability: str) -> tuple[str, ...]:
    mapped = _CAPABILITY_TO_CLAUDE_TOOLS.get(capability.lower())
    if mapped is not None:
        return mapped
    if not capability:
        return ()
    return (capability[:1].upper() + capability[1:],)


def _compile_tool_rules(rules: Sequence[ToolRule]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(allowed, disallowed)``; ``ask`` counts as ``allow`` (no flag expresses it)."""

    default_action = next(
        (rule.action for rule in rules if rule.capability == "*" and rule.pattern is None),
        None,
    )
    allowed: list[str] = []
    disallowed: list[str] = []
    known_all_tools = dedupe_nonempty(
        tool for tools in _CAPABILITY_TO_CLAUDE_TOOLS.values() for tool in tools
    )

    for rule in rules:
        if rule.capability is None or (rule.capability == "*" and rule.pattern is None):
            continue
        mapped = _claude_tools_for_capability(rule.capability)
        if rule.action in {"allow", "ask"}:
            target = allowed
        elif rule.action == "deny" and default_action != "deny":
            target = disallowed
        else:
            continue
        if rule.pattern is None:
            target.extend(mapped)
        else:
            target.extend(f"{tool}({rule.pattern})" for tool in mapped)

    if default_action == "deny" and not allowed:
        # Best-effort representation for "deny everything".
        disallowed.extend(known_all_tools)

    return tuple(dedupe_nonempty(allowed)), tuple(dedupe_nonempty(disallowed))


def compile_claude_permissions(spec: ResolvedLaunchSpec) -> ClaudePermissions:
    """Compile ``spec.permission_policy`` for Claude."""

    policy = spec.permission_policy
    allowed, disallowed = _compile_tool_rules(policy.rules)
    return ClaudePermissions(
        skip_permission_prompts=policy.approval in ("yolo", "never"),
        permission_mode=(
            "acceptEdits"
            if policy.approval == "auto"
            else "default"
            if policy.approval == "confirm"
            else None
        ),
        allowed_tools=allowed,
        disallowed_tools=disallowed,
    )


__all__ = ["ClaudePermissions", "compile_claude_permissions"]
