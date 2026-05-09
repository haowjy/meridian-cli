"""Codex subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import cast

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.project_codex_common import (
    map_codex_approval_policy,
    map_codex_sandbox_mode,
    project_codex_mcp_config_flags,
)
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "prompt",
        "continue_session_id",
        "continue_fork",
        "permission_resolver",
        "extra_args",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "report_output_path",
        "base_instructions",
        "developer_instructions",
        "user_turn_content",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "agent_name",
        "agents_payload",
        "appended_system_prompt",
        "prompt_file_path",
        "reference_items",
        "skills",
    }
)


def _coerce_permission_flags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, tuple):
        return tuple(str(token) for token in cast("tuple[object, ...]", raw))
    if isinstance(raw, list):
        return tuple(str(token) for token in cast("list[object]", raw))
    if isinstance(raw, Iterable):
        return tuple(str(token) for token in cast("Iterable[object]", raw))
    raise TypeError(f"Permission resolver flags must be iterable, got {type(raw).__name__}")


def _strip_tool_flags_for_codex(flags: tuple[str, ...]) -> tuple[str, ...]:
    """Strip --allowedTools/--disallowedTools flags that Codex doesn't support."""
    filtered: list[str] = []
    index = 0
    while index < len(flags):
        token = flags[index]

        if token == "--allowedTools":
            index += 2
            continue
        if token.startswith("--allowedTools="):
            index += 1
            continue
        if token == "--disallowedTools":
            index += 2
            continue
        if token.startswith("--disallowedTools="):
            index += 1
            continue

        filtered.append(token)
        index += 1

    return tuple(filtered)


def project_codex_permission_flags(permission_resolver: PermissionResolver) -> tuple[str, ...]:
    """Build Codex subprocess permission flags from resolver config and hints."""

    config = permission_resolver.config
    flags: list[str] = []

    sandbox_mode = map_codex_sandbox_mode(config.sandbox)
    if sandbox_mode is not None:
        flags.extend(("--sandbox", sandbox_mode))

    approval_policy = map_codex_approval_policy(config.approval)
    if approval_policy is not None:
        flags.extend(("-c", f'approval_policy={json.dumps(approval_policy)}'))

    resolver_flags = _strip_tool_flags_for_codex(
        _coerce_permission_flags(permission_resolver.resolve_flags())
    )
    flags.extend(resolver_flags)
    return tuple(flags)


def project_codex_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered subprocess command list."""

    command: list[str] = list(base_command)
    prompt_parts = (
        spec.base_instructions,
        spec.developer_instructions,
        spec.prompt,
    )
    final_prompt = "\n\n".join(part.strip() for part in prompt_parts if part and part.strip())

    harness_session_id = (spec.continue_session_id or "").strip()
    if spec.interactive:
        guarded_prompt = final_prompt
        if guarded_prompt and not harness_session_id:
            guarded_prompt = f"{guarded_prompt}\n\nDO NOT DO ANYTHING. WAIT FOR USER INPUT."
    else:
        guarded_prompt = final_prompt

    if spec.model is not None:
        command.extend(("--model", spec.model))

    normalized_effort = (spec.effort or "").strip()
    if normalized_effort:
        command.extend(("-c", f"model_reasoning_effort={json.dumps(normalized_effort)}"))

    command.extend(project_codex_permission_flags(spec.permission_resolver))
    command.extend(project_codex_mcp_config_flags(spec.mcp_tools))

    if harness_session_id:
        command.extend(("resume", harness_session_id))

    for root in spec.projected_roots:
        command.extend(("--add-dir", root.as_posix()))

    command.extend(spec.extra_args)

    if not spec.interactive and spec.report_output_path:
        command.extend(("-o", spec.report_output_path))

    if guarded_prompt:
        command.append(guarded_prompt)

    return command


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_PROJECTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "_DELEGATED_FIELDS",
    "_PROJECTED_FIELDS",
    "_check_projection_drift",
    "project_codex_permission_flags",
    "project_codex_spec_to_cli_args",
]
