"""Codex streaming projections for app-server command and thread bootstrap."""

from __future__ import annotations

import json
import logging

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.project_codex_common import (
    HarnessCapabilityMismatch,
    map_codex_approval_policy,
    map_codex_sandbox_mode,
    project_codex_mcp_config_flags,
)
from meridian.lib.launch.constants import BASE_COMMAND_CODEX_STREAMING
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_APP_SERVER_ARG_FIELDS: frozenset[str] = frozenset(
    {
        "permission_resolver",
        "extra_args",
        "projected_roots",
        "report_output_path",
        "mcp_tools",
    }
)

_JSONRPC_PARAM_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "permission_resolver",
    }
)

_METHOD_SELECTION_FIELDS: frozenset[str] = frozenset(
    {
        "continue_session_id",
        "continue_fork",
    }
)

_INSTRUCTION_FIELDS: frozenset[str] = frozenset(
    {
        "base_instructions",
        "developer_instructions",
        "user_turn_content",
    }
)

_LIFECYCLE_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "interactive",
    }
)

_ACCOUNTED_FIELDS: frozenset[str] = (
    _APP_SERVER_ARG_FIELDS
    | _JSONRPC_PARAM_FIELDS
    | _METHOD_SELECTION_FIELDS
    | _INSTRUCTION_FIELDS
    | _LIFECYCLE_FIELDS
)
_PROJECTED_FIELDS: frozenset[str] = _ACCOUNTED_FIELDS
_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "agent_name",
        "agents_payload",
        "claude_native_agents_enabled",
        "appended_system_prompt",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "prompt_file_path",
        "reference_items",
        "skills",
        "task_cwd",
    }
)


def _build_writable_roots_config(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Build a ``-c sandbox_workspace_write.writable_roots=[...]`` flag pair.

    Returns an empty tuple when *paths* is empty so callers can extend
    unconditionally.
    """
    if not paths:
        return ()
    paths_json = json.dumps(list(paths))
    return ("-c", f"sandbox_workspace_write.writable_roots={paths_json}")


def _select_thread_method(spec: ResolvedLaunchSpec) -> str:
    resume_thread_id = (spec.continue_session_id or "").strip()
    if not resume_thread_id:
        return "thread/start"
    if spec.continue_fork:
        return "thread/fork"
    return "thread/resume"


def _consume_streaming_lifecycle_fields(spec: ResolvedLaunchSpec) -> None:
    # Prompt is sent in codex_ws after thread bootstrap, but we still account
    # for the field in this projection module to keep drift checks complete.
    _ = spec.prompt
    if spec.interactive:
        logger.debug(
            "Codex streaming ignores interactive launch flag; "
            "websocket transport remains interactive"
        )


def project_codex_spec_to_appserver_command(
    spec: ResolvedLaunchSpec,
    *,
    host: str,
    port: int,
) -> list[str]:
    """Build one ``codex app-server`` command from ``ResolvedLaunchSpec``."""

    _consume_streaming_lifecycle_fields(spec)

    command: list[str] = [
        *BASE_COMMAND_CODEX_STREAMING,
        "--listen",
        f"ws://{host}:{port}",
    ]

    sandbox_mode = map_codex_sandbox_mode(spec.permission_resolver.config.sandbox)
    if sandbox_mode is not None:
        command.extend(("-c", f"sandbox_mode={json.dumps(sandbox_mode)}"))

    approval_policy = map_codex_approval_policy(spec.permission_resolver.config.approval)
    if approval_policy is not None:
        command.extend(("-c", f"approval_policy={json.dumps(approval_policy)}"))

    command.extend(project_codex_mcp_config_flags(spec.mcp_tools))

    if spec.report_output_path is not None:
        logger.debug("Codex streaming ignores report_output_path; reports extracted from artifacts")

    writable_roots_config = _build_writable_roots_config(
        tuple(root.as_posix() for root in spec.projected_roots)
    )
    if writable_roots_config:
        logger.debug(
            "Projecting roots to sandbox_workspace_write.writable_roots: %s",
            [root.as_posix() for root in spec.projected_roots],
        )
        command.extend(writable_roots_config)

    if spec.extra_args:
        logger.debug(
            "Forwarding passthrough args to codex app-server: %s",
            list(spec.extra_args),
        )
        command.extend(spec.extra_args)

    return command


def project_codex_spec_to_thread_request(
    spec: ResolvedLaunchSpec,
    *,
    cwd: str,
) -> tuple[str, dict[str, object]]:
    """Build thread bootstrap method+payload for the Codex app-server JSON-RPC."""

    _consume_streaming_lifecycle_fields(spec)

    payload: dict[str, object] = {"cwd": cwd}

    if spec.base_instructions:
        payload["baseInstructions"] = spec.base_instructions

    if spec.developer_instructions:
        payload["developerInstructions"] = spec.developer_instructions

    if spec.model:
        payload["model"] = spec.model

    normalized_effort = (spec.effort or "").strip()
    if normalized_effort:
        payload["config"] = {"model_reasoning_effort": normalized_effort}

    approval_policy = map_codex_approval_policy(spec.permission_resolver.config.approval)
    if approval_policy is not None:
        payload["approvalPolicy"] = approval_policy

    sandbox_mode = map_codex_sandbox_mode(spec.permission_resolver.config.sandbox)
    if sandbox_mode is not None:
        payload["sandbox"] = sandbox_mode

    method = _select_thread_method(spec)
    resume_thread_id = (spec.continue_session_id or "").strip()
    if resume_thread_id:
        payload["threadId"] = resume_thread_id

    if method == "thread/fork":
        # Explicitly pin fork behavior to non-ephemeral sessions for parity
        # with subprocess continuation semantics.
        payload.setdefault("ephemeral", False)

    return method, payload


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_ACCOUNTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "_ACCOUNTED_FIELDS",
    "_APP_SERVER_ARG_FIELDS",
    "_DELEGATED_FIELDS",
    "_INSTRUCTION_FIELDS",
    "_JSONRPC_PARAM_FIELDS",
    "_LIFECYCLE_FIELDS",
    "_METHOD_SELECTION_FIELDS",
    "_PROJECTED_FIELDS",
    "HarnessCapabilityMismatch",
    "_build_writable_roots_config",
    "_check_projection_drift",
    "project_codex_spec_to_appserver_command",
    "project_codex_spec_to_thread_request",
]
