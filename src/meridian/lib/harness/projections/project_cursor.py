"""Cursor subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.permissions_cursor import compile_cursor_permissions
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "prompt",
        "permission_policy",
        "extra_args",
        "task_cwd",
        "continue_session_id",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "native_identity",
        "harness",
        "continue_fork",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "effort",
        "agent_name",
        "appended_system_prompt",
        "agents_payload",
        "claude_runtime",
        "prompt_file_path",
        "user_turn_content",
        "report_output_path",
        "base_instructions",
        "developer_instructions",
        "skills",
        "reference_items",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "opencode_version",
    }
)


def _assert_supported_for_mvp(spec: ResolvedLaunchSpec) -> None:
    if spec.mcp_tools:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess does not support per-spawn mcp_tools for MVP."
        )

    if spec.continue_fork:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess continue_fork is not supported for MVP."
        )

    if spec.interactive:
        raise HarnessCapabilityMismatch(
            "Cursor subprocess interactive mode is not supported for MVP."
        )


def project_cursor_spec_to_cli_args(
    spec: ResolvedLaunchSpec,
    *,
    base_command: tuple[str, ...],
) -> list[str]:
    """Project one ``ResolvedLaunchSpec`` into an ordered subprocess command list."""

    _assert_supported_for_mvp(spec)
    if spec.projected_roots:
        logger.debug(
            "Cursor subprocess received projected_roots but MVP only projects --workspace "
            "from task_cwd; ignoring extra workspace roots."
        )

    command: list[str] = list(base_command)

    if spec.model is not None:
        command.extend(("--model", spec.model))

    permissions = compile_cursor_permissions(spec)
    if permissions.approval is not None:
        command.append(f"--{permissions.approval}")
    if permissions.sandbox is not None:
        command.extend(("--sandbox", permissions.sandbox))

    chat_id = (spec.continue_session_id or "").strip()
    if chat_id:
        command.extend(("--resume", chat_id))

    workspace = (spec.task_cwd or "").strip()
    if workspace:
        command.extend(("--workspace", workspace))

    if spec.extra_args:
        command.extend(spec.extra_args)

    command.append(spec.prompt)
    return command


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_PROJECTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "HarnessCapabilityMismatch",
    "project_cursor_spec_to_cli_args",
]
