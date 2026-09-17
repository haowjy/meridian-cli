"""OpenCode streaming projections for serve command and session payload."""

from __future__ import annotations

import json
import logging
from typing import Literal, cast

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

_MESSAGE_FIELDS: frozenset[str] = frozenset({"appended_system_prompt", "model"})
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
        "web_search_enabled",
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


def opencode_model_parts(model: str) -> tuple[str, str]:
    provider, separator, model_id = model.partition("/")
    if not separator or not provider.strip() or not model_id.strip():
        raise HarnessCapabilityMismatch(
            "OpenCode requires a provider-qualified model: provider/model"
        )
    return provider, model_id


def project_opencode_model(
    model: str | None, *, id_field: Literal["id", "modelID"],
) -> dict[str, str] | None:
    """Project the provider-qualified selection to OpenCode's endpoint-specific model ref."""
    if model is None:
        return None
    provider, model_id = opencode_model_parts(model)
    return {"providerID": provider, id_field: model_id}


def project_opencode_model_config(raw: str | None, model: str, agent: str | None = None) -> str:
    """Launch-local override; preserve native agent fields and reject malformed input."""
    opencode_model_parts(model)
    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
    config["model"] = model
    if agent is not None:
        agents = config.get("agent", {})
        if not isinstance(agents, dict):
            raise HarnessCapabilityMismatch("OpenCode agent configuration must be a JSON object")
        agents = dict(cast("dict[str, object]", agents))
        selected = agents.get(agent, {})
        if not isinstance(selected, dict):
            raise HarnessCapabilityMismatch(
                "OpenCode selected-agent configuration must be a JSON object"
            )
        agents[agent] = {**cast("dict[str, object]", selected), "model": model}
        config["agent"] = agents
    return json.dumps(config, separators=(",", ":"))


def project_opencode_spec_to_session_payload(spec: ResolvedLaunchSpec) -> dict[str, object]:
    """Build session-creation payload for the OpenCode HTTP API."""

    _consume_streaming_lifecycle_fields(spec)

    if spec.continue_fork:
        raise HarnessCapabilityMismatch(
            "OpenCode streaming cannot express continue_fork semantics over "
            "the current /session API."
        )

    payload: dict[str, object] = {}

    model = project_opencode_model(spec.model, id_field="id")
    if model is not None:
        payload["model"] = model

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
    "project_opencode_model",
    "project_opencode_spec_to_serve_command",
    "project_opencode_spec_to_session_payload",
]
