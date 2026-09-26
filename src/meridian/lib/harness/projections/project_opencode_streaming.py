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
        "native_identity",
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
        "opencode_version",
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


_OPENCODE_V2_ACTION_ALIASES: dict[str, str] = {
    "bash": "shell",
    "write": "edit",
    "patch": "edit",
    "task": "subagent",
}
"""V1 capability names that OpenCode V2 normalizes to a canonical action."""

_OPENCODE_V2_PERMISSION_EFFECTS: frozenset[str] = frozenset({"allow", "deny", "ask"})

_OPENCODE_V2_EFFECT_PRECEDENCE: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}


def _split_opencode_v2_permission_key(raw_key: str) -> tuple[str, str | None]:
    """Split a V1 ``capability(pattern)`` permission key into its parts."""

    key = raw_key.strip()
    scoped_start = key.find("(")
    if scoped_start <= 0 or not key.endswith(")"):
        return key, None
    return key[:scoped_start].strip(), key[scoped_start + 1 : -1]


def _opencode_v2_permission_rule(
    capability: str, resource: str | None, effect: object
) -> dict[str, str]:
    normalized_effect = str(effect).strip().lower()
    if normalized_effect not in _OPENCODE_V2_PERMISSION_EFFECTS:
        raise HarnessCapabilityMismatch(
            f"OpenCode permission effect must be allow/deny/ask, got '{effect}'"
        )
    action = _OPENCODE_V2_ACTION_ALIASES.get(capability.strip().lower(), capability.strip())
    return {
        "action": action,
        "resource": (resource or "*").strip() or "*",
        "effect": normalized_effect,
    }


def project_opencode_v2_permissions(override_json: str | None) -> list[dict[str, str]]:
    """Translate Meridian's compiled OpenCode permission map to V2 native rules.

    ``override_json`` is the flat ``{capability: action}`` map emitted by
    ``compile_tools_to_opencode_permission``. Scoped keys (``bash(git status)``)
    become ``action``/``resource`` pairs. Order is preserved so a V2 last-match
    evaluator keeps broad rules before the exceptions that refine them.
    """

    if not override_json or not override_json.strip():
        return []
    parsed: object = json.loads(override_json)
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode permission override must be a JSON object")
    rules: list[dict[str, str]] = []
    for raw_key, raw_action in cast("dict[str, object]", parsed).items():
        capability, resource = _split_opencode_v2_permission_key(str(raw_key))
        rules.append(_opencode_v2_permission_rule(capability, resource, raw_action))
    return rules


def _resolve_opencode_v2_rule_collisions(
    rules: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Collapse rules that share one ``(action, resource)`` onto the strongest effect.

    V2 resolves the last matching rule, so distinct V1 capabilities that alias to
    the same canonical action (``write``/``patch`` → ``edit``) would otherwise let
    emission order decide the outcome. ``deny > ask > allow`` wins regardless of
    order. Only identical ``(action, resource)`` keys collapse; overlapping-but-
    different resources keep their order so a scoped allow still refines a broad
    deny.
    """

    if not rules:
        return rules
    strongest: dict[tuple[str, str], str] = {}
    last_index: dict[tuple[str, str], int] = {}
    for index, rule in enumerate(rules):
        key = (rule["action"], rule["resource"])
        effect = rule["effect"]
        previous = strongest.get(key)
        if previous is not None and previous != effect:
            logger.warning(
                "OpenCode V2 permission collision on %s(%s): %s vs %s; "
                "keeping the stronger effect",
                key[0],
                key[1],
                previous,
                effect,
            )
        if previous is None or (
            _OPENCODE_V2_EFFECT_PRECEDENCE[effect]
            > _OPENCODE_V2_EFFECT_PRECEDENCE[previous]
        ):
            strongest[key] = effect
        last_index[key] = index
    resolved: list[dict[str, str]] = []
    for index, rule in enumerate(rules):
        key = (rule["action"], rule["resource"])
        if last_index[key] != index:
            continue
        resolved.append({"action": key[0], "resource": key[1], "effect": strongest[key]})
    return resolved


def _order_opencode_v2_rules_broad_first(
    rules: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Stable-partition rules so broad resources precede specific ones.

    V2 resolves the last matching rule, so a scoped rule only refines a broad
    rule when it is emitted after it. A broad aliased ``allow`` (``write`` →
    ``edit/*``) declared after a scoped ``deny`` (``edit(foo)``) would otherwise
    let declaration order decide the outcome. Python's sort is stable, so rules
    of equal broadness keep their authored relative order.
    """

    return sorted(rules, key=lambda rule: rule["resource"] != "*")


def _project_opencode_v1_permission_to_v2_rules(permission: object) -> list[dict[str, str]]:
    """Convert a V1 ``permission`` map (scalar or nested-pattern) to V2 rules."""

    if not isinstance(permission, dict):
        return []
    rules: list[dict[str, str]] = []
    for raw_capability, value in cast("dict[str, object]", permission).items():
        capability, scoped_resource = _split_opencode_v2_permission_key(str(raw_capability))
        if isinstance(value, dict):
            for raw_pattern, effect in cast("dict[str, object]", value).items():
                resource = scoped_resource if scoped_resource is not None else str(raw_pattern)
                rules.append(_opencode_v2_permission_rule(capability, resource, effect))
        else:
            rules.append(_opencode_v2_permission_rule(capability, scoped_resource, value))
    return rules


def _split_inherited_opencode_v1_permission_rules(
    permission: object,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split inherited V1 rules into non-root rules and ``external_directory`` grants."""

    rules = _project_opencode_v1_permission_to_v2_rules(permission)
    external_directory = [rule for rule in rules if rule["action"] == "external_directory"]
    non_root = [rule for rule in rules if rule["action"] != "external_directory"]
    return non_root, external_directory


def merge_opencode_v2_permission_config(
    raw: str | None, override_json: str | None
) -> str | None:
    """Inject V2-native permissions derived from Meridian's tools policy.

    Returns ``raw`` unchanged when there is no policy to apply. Existing native
    ``permissions`` stay first, then inherited V1 ``permission`` entries, then
    Meridian's tools rules, then inherited ``external_directory`` grants last.
    V2 resolves the last matching rule, so Meridian's tools policy wins over any
    inherited non-root rule while an explicit root grant is still never shadowed
    by a broad tools ``deny``. Rules that alias to the same ``(action, resource)``
    are collapsed to their strongest effect, and each Meridian-composed group is
    stable-sorted broad-first (``resource == "*"`` before specific resources) so
    a scoped deny is not order-dependent on a broad aliased allow. The parent
    ``existing_permissions`` and the ``external_directory`` root grants are
    emitted as authored.
    """

    rules = project_opencode_v2_permissions(override_json)
    if not rules:
        return raw
    parsed: object = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(parsed, dict):
        raise HarnessCapabilityMismatch("OpenCode config content must be a JSON object")
    config = dict(cast("dict[str, object]", parsed))
    existing_permissions = config.pop("permissions", [])
    if not isinstance(existing_permissions, list):
        raise HarnessCapabilityMismatch("OpenCode permissions must be a list of rules")
    v1_permission = config.pop("permission", None)
    inherited_non_root, inherited_external_directory = (
        _split_inherited_opencode_v1_permission_rules(v1_permission)
    )
    config["permissions"] = [
        *cast("list[object]", existing_permissions),
        *_order_opencode_v2_rules_broad_first(
            _resolve_opencode_v2_rule_collisions(inherited_non_root)
        ),
        *_order_opencode_v2_rules_broad_first(_resolve_opencode_v2_rule_collisions(rules)),
        *inherited_external_directory,
    ]
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
    "merge_opencode_v2_permission_config",
    "project_opencode_model",
    "project_opencode_spec_to_serve_command",
    "project_opencode_spec_to_session_payload",
    "project_opencode_v2_permissions",
]
