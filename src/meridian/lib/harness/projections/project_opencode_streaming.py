"""OpenCode streaming projections for serve command and session payload."""

from __future__ import annotations

import logging

from meridian.lib.harness.projections._guards import (
    check_projection_drift as _check_projection_drift,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.constants import BASE_COMMAND_OPENCODE_STREAMING
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

logger = logging.getLogger(__name__)

_SERVE_COMMAND_FIELDS: frozenset[str] = frozenset(
    {
        "extra_args",
        "projected_roots",
        "interactive",
        "prompt",
        "permission_resolver",
    }
)

_SESSION_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "effort",
        "continue_session_id",
        "continue_fork",
        "agent_name",
        "skills",
        "mcp_tools",
    }
)

_MESSAGE_FIELDS: frozenset[str] = frozenset({"appended_system_prompt"})
_REFERENCE_FIELDS: frozenset[str] = frozenset({"reference_items"})

_ACCOUNTED_FIELDS: frozenset[str] = (
    _SERVE_COMMAND_FIELDS | _SESSION_PAYLOAD_FIELDS | _MESSAGE_FIELDS | _REFERENCE_FIELDS
)
_PROJECTED_FIELDS: frozenset[str] = _ACCOUNTED_FIELDS
_DELEGATED_FIELDS: frozenset[str] = frozenset(
    {
        "agents_payload",
        "claude_native_agents_enabled",
        "base_instructions",
        "developer_instructions",
        "harness",
        "pi_extension_entrypoints",
        "load_all_pi_extensions",
        "disallowed_tools",
        "prompt_file_path",
        "report_output_path",
        "task_cwd",
        "user_turn_content",
    }
)


def _consume_streaming_lifecycle_fields(spec: ResolvedLaunchSpec) -> None:
    _ = spec.prompt
    _ = spec.appended_system_prompt
    if spec.reference_items:
        logger.debug(
            "OpenCode streaming ignores native reference_items; "
            "reference content must be delivered by prompt injection"
        )
    if spec.interactive:
        logger.debug(
            "OpenCode streaming ignores interactive launch flag; HTTP transport remains interactive"
        )
    if spec.projected_roots:
        logger.debug(
            "OpenCode streaming workspace roots are projected via %s env.",
            "OPENCODE_CONFIG_CONTENT",
        )

    config = spec.permission_resolver.config
    if config.approval != "default" or config.sandbox != "default":
        logger.debug(
            "OpenCode streaming ignores permission resolver overrides; "
            "opencode serve has no launch-time permission mapping"
        )


def project_opencode_spec_to_serve_command(
    spec: ResolvedLaunchSpec,
    *,
    host: str,
    port: int,
) -> list[str]:
    """Build one ``opencode serve`` command from ``ResolvedLaunchSpec``."""

    _consume_streaming_lifecycle_fields(spec)

    command: list[str] = [
        *BASE_COMMAND_OPENCODE_STREAMING,
        "--hostname",
        host,
        "--port",
        str(port),
    ]

    if spec.extra_args:
        logger.debug(
            "Forwarding passthrough args to opencode serve: %s",
            list(spec.extra_args),
        )
        command.extend(spec.extra_args)

    return command


def project_opencode_spec_to_session_payload(spec: ResolvedLaunchSpec) -> dict[str, object]:
    """Build session-creation payload for the OpenCode HTTP API."""

    _consume_streaming_lifecycle_fields(spec)

    if spec.continue_fork:
        raise HarnessCapabilityMismatch(
            "OpenCode streaming cannot express continue_fork semantics over "
            "the current /session API."
        )

    payload: dict[str, object] = {}

    if spec.model is not None:
        payload["model"] = spec.model
        payload["modelID"] = spec.model

    normalized_effort = (spec.effort or "").strip()
    if normalized_effort:
        logger.debug(
            "OpenCode streaming does not support effort override; ignoring effort=%s",
            normalized_effort,
        )

    if spec.agent_name:
        payload["agent"] = spec.agent_name

    if spec.skills:
        payload["skills"] = list(spec.skills)

    projected_mcp_tools = [entry.strip() for entry in spec.mcp_tools if entry.strip()]
    if projected_mcp_tools:
        payload["mcp"] = {"servers": projected_mcp_tools}

    # Note: we intentionally do NOT pass continue_session_id to the server
    # via payload. POST /session ignores sessionID and always creates a new
    # empty session. Continuation is handled in opencode_http.py by verifying
    # the existing session via GET /session/{id} before attempting POST.

    return payload


_check_projection_drift(
    ResolvedLaunchSpec,
    projected=_ACCOUNTED_FIELDS,
    delegated=_DELEGATED_FIELDS,
)


__all__ = [
    "_ACCOUNTED_FIELDS",
    "_DELEGATED_FIELDS",
    "_MESSAGE_FIELDS",
    "_PROJECTED_FIELDS",
    "_REFERENCE_FIELDS",
    "_SERVE_COMMAND_FIELDS",
    "_SESSION_PAYLOAD_FIELDS",
    "HarnessCapabilityMismatch",
    "_check_projection_drift",
    "project_opencode_spec_to_serve_command",
    "project_opencode_spec_to_session_payload",
]
