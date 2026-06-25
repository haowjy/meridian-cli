"""Cursor subprocess command projection from ``ResolvedLaunchSpec``."""

from __future__ import annotations

import logging

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_PROJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "prompt",
        "permission_resolver",
        "extra_args",
        "task_cwd",
        "continue_session_id",
    }
)

_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "harness",
        "continue_fork",
        "interactive",
        "mcp_tools",
        "projected_roots",
        "effort",
        "agent_name",
        "appended_system_prompt",
        "agents_payload",
        "claude_native_agents_enabled",
        "prompt_file_path",
        "user_turn_content",
        "report_output_path",
        "base_instructions",
        "developer_instructions",
        "web_search_enabled",
        "skills",
        "reference_items",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "disallowed_tools",
    }
)


def _project_approval_flags(spec: ResolvedLaunchSpec) -> tuple[str, ...]:
    approval_mode = spec.permission_resolver.config.approval
    if approval_mode in ("yolo", "never"):
        return ("--yolo",)
    if approval_mode == "auto":
        return ("--force",)
    if approval_mode in {"default", "confirm"}:
        return ()
    raise HarnessCapabilityMismatch(
        f"Cursor projection does not support approval mode '{approval_mode}'."
    )


def _project_sandbox_flags(spec: ResolvedLaunchSpec) -> tuple[str, ...]:
    """Map mars sandbox mode to Cursor ``--sandbox enabled|disabled``.

    Cursor's sandbox is binary — ``enabled`` restricts file access to the
    workspace, ``disabled`` removes restrictions.  Mars ``workspace-write``
    and ``danger-full-access`` both map to ``disabled`` (overshoot for
    ``workspace-write``, but Cursor has no fine-grained equivalent).
    """
    sandbox_mode = spec.permission_resolver.config.sandbox
    if sandbox_mode == "default":
        return ()
    if sandbox_mode == "read-only":
        return ("--sandbox", "enabled")
    if sandbox_mode in ("workspace-write", "danger-full-access"):
        return ("--sandbox", "disabled")
    raise HarnessCapabilityMismatch(
        f"Cursor projection does not support sandbox mode '{sandbox_mode}'."
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

    command.extend(_project_approval_flags(spec))
    command.extend(_project_sandbox_flags(spec))

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
