"""Codex streaming projections for app-server command and thread bootstrap."""

from __future__ import annotations

import json
import logging

from meridian.lib.harness.codex_idle import IDLE_NOTIFY_COMMAND
from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.compaction import project_codex_autocompact_args
from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_APPROVAL_POLICY_KEY,
    CODEX_CONFIG_NOTIFY_KEY,
    CODEX_CONFIG_REASONING_EFFORT_KEY,
    project_codex_native_config,
)
from meridian.lib.harness.projections.permissions_codex import compile_codex_permissions
from meridian.lib.harness.projections.project_codex_common import (
    CODEX_DISABLE_BUILTIN_AGENT_FLAGS,
    project_codex_sandbox,
    project_codex_tool_switch_flags,
)
from meridian.lib.launch.constants import BASE_COMMAND_CODEX_STREAMING
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_APP_SERVER_ARG_FIELDS: frozenset[str] = frozenset(
    {
        "permission_policy",
        "native_config",
        "extra_args",
        "projected_roots",
        "report_output_path",
    }
)

_JSONRPC_PARAM_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "autocompact",
        "autocompact_pct",
        "permission_policy",
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
        "native_identity",
        "agent_name",
        "agents_payload",
        "claude_runtime",
        "codex_runtime",
        "appended_system_prompt",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "opencode_version",
        "prompt_file_path",
        "reference_items",
        "skills",
        "task_cwd",
    }
)


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


# Flags accepted by `codex app-server` (from `codex app-server --help`). Unlike
# `codex exec`, app-server takes almost nothing as a bare flag — model/sandbox/etc.
# must be `-c key=value` config overrides (or per-WS-request). Any other bare flag
# (e.g. the exec-only `--search`) crashes the transport with "unexpected argument".
# `APPSERVER_ACCEPTED_FLAGS` is the contract; the guard test asserts the builder
# only ever emits flags from this set. Keep in sync with the codex CLI.
APPSERVER_ACCEPTED_FLAGS: frozenset[str] = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--strict-config",
        "--listen",
        "--stdio",
        "--analytics-default-enabled",
        "--ws-auth",
        "--ws-token-file",
        "--ws-token-sha256",
        "--ws-shared-secret-file",
        "--ws-issuer",
        "--ws-audience",
        "--ws-max-clock-skew-seconds",
        "-h",
        "--help",
    }
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
    command.extend(project_codex_autocompact_args(spec.autocompact, spec.autocompact_pct))

    if spec.interactive:
        command.extend(("-c", f"{CODEX_CONFIG_NOTIFY_KEY}={json.dumps(IDLE_NOTIFY_COMMAND)}"))

    permissions, _ = compile_codex_permissions(spec)
    sandbox = project_codex_sandbox(spec, permissions)
    command.extend(sandbox.appserver_args())

    if permissions.approval_policy is not None:
        command.extend(
            (
                "-c",
                f"{CODEX_CONFIG_APPROVAL_POLICY_KEY}={json.dumps(permissions.approval_policy)}",
            )
        )

    command.extend(project_codex_tool_switch_flags(permissions))

    if spec.report_output_path is not None:
        logger.debug(
            "Codex streaming ignores report_output_path; reports extracted from stream events"
        )

    if sandbox.writable_roots:
        logger.debug(
            "Projecting roots to sandbox_workspace_write.writable_roots: %s",
            list(sandbox.writable_roots),
        )

    native_config_args, _ = project_codex_native_config(spec.native_config)
    command.extend(native_config_args)

    # Ahead of passthrough args so a later `-c` override still wins.
    if permissions.builtin_agents_disabled:
        command.extend(CODEX_DISABLE_BUILTIN_AGENT_FLAGS)

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
    thread_config: dict[str, object] = {}
    if normalized_effort:
        thread_config[CODEX_CONFIG_REASONING_EFFORT_KEY] = normalized_effort

    permissions, _ = compile_codex_permissions(spec)
    sandbox = project_codex_sandbox(spec, permissions)
    if permissions.approval_policy is not None:
        payload["approvalPolicy"] = permissions.approval_policy

    sandbox_fields, sandbox_config = sandbox.thread_fields()
    payload.update(sandbox_fields)
    thread_config.update(sandbox_config)
    if thread_config:
        payload["config"] = thread_config

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
    "_check_projection_drift",
    "project_codex_spec_to_appserver_command",
    "project_codex_spec_to_thread_request",
]
