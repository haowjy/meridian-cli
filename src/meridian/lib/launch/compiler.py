"""Pure-data compiler contract for launch parameter resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Literal, cast

from meridian.lib.catalog.agent import AgentModelEntry, ModelPolicyRule
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import AgentOverlayConfig
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.overrides import (
    EXECUTION_POLICY_FIELDS,
    ExecutionPolicyField,
    RuntimeOverrides,
    normalize_execution_policy_fields,
)
from meridian.lib.tools import ToolsField


class ProvenanceLevel(Enum):
    CLI = "cli"
    ENV = "env"
    AGENT_OVERLAY_POLICY = "agent-overlay-model-policy"
    AGENT_OVERLAY_DEFAULT = "agent-overlay-default"
    PROFILE_MODEL_POLICY = "profile-model-policy"
    PROFILE_DEFAULT = "profile-default"
    CONFIG_DEFAULT = "config-default"
    ALIAS_DEFAULT = "alias-default"
    UNSET = "unset"


@dataclass(frozen=True)
class FieldProvenance:
    """Records where each effective field value came from."""

    model_source: ProvenanceLevel = ProvenanceLevel.UNSET
    harness_source: ProvenanceLevel = ProvenanceLevel.UNSET
    effort_source: ProvenanceLevel = ProvenanceLevel.UNSET
    approval_source: ProvenanceLevel = ProvenanceLevel.UNSET
    sandbox_source: ProvenanceLevel = ProvenanceLevel.UNSET
    autocompact_source: ProvenanceLevel = ProvenanceLevel.UNSET
    autocompact_pct_source: ProvenanceLevel = ProvenanceLevel.UNSET
    timeout_source: ProvenanceLevel = ProvenanceLevel.UNSET


@dataclass(frozen=True)
class CompilerRequest:
    """Pure-data input to the compiler. JSON-serializable."""

    # Identity
    requested_agent: str | None

    # Layered override sources — separate for provenance accuracy
    cli_overrides: RuntimeOverrides
    env_overrides: RuntimeOverrides
    agent_overlay: AgentOverlayConfig | None
    config_defaults: RuntimeOverrides

    # Agent profile data (loaded by caller, passed as data)
    profile_routing_model: str | None
    profile_routing_harness: str | None
    profile_policy_defaults: ResolvedExecutionPolicy
    profile_model_policies: tuple[ModelPolicyRule, ...] | None  # None = no profile
    profile_legacy_models: dict[str, AgentModelEntry] | None
    profile_skills: tuple[str, ...]

    # Model catalog data
    resolved_alias_entry: AliasEntry | None
    alias_catalog: dict[str, AliasEntry]

    # Options
    configured_default_harness: str = "claude"
    project_root: str = ""
    supported_execution_policy_fields: tuple[ExecutionPolicyField, ...] = EXECUTION_POLICY_FIELDS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported_execution_policy_fields",
            normalize_execution_policy_fields(self.supported_execution_policy_fields),
        )


@dataclass(frozen=True)
class CompilerResult:
    """Pure-data compiler output. JSON-serializable."""

    # Identity
    agent_name: str | None

    # Model + routing
    model: str
    model_token: str
    harness: str

    # Runtime policy
    execution_policy: ResolvedExecutionPolicy

    # Profile-derived content identifiers
    skill_names: tuple[str, ...]
    tools: ToolsField | None = None
    mcp_tools: tuple[str, ...] | None = None

    # Selection context
    model_selection_requested_token: str = ""
    model_selection_canonical_id: str = ""
    harness_provenance: str = ""

    # Provenance + diagnostics
    field_provenance: FieldProvenance = field(default_factory=FieldProvenance)
    warnings: tuple[str, ...] = ()

    # Fallback info
    fallback_chain: tuple[dict[str, object], ...] = ()
    model_policy_source: ProvenanceLevel = ProvenanceLevel.UNSET
    matched_model_policy: bool = False


def render_provenance(provenance: FieldProvenance) -> dict[str, str]:
    """Render provenance to a human-readable mapping for dry-run/diagnostic output."""
    output: dict[str, str] = {}
    for field_name in (
        "model",
        "harness",
        "effort",
        "approval",
        "sandbox",
        "autocompact",
        "autocompact_pct",
        "timeout",
    ):
        source_attr = f"{field_name}_source"
        level = getattr(provenance, source_attr)
        if level is not ProvenanceLevel.UNSET:
            output[field_name] = level.value
    return output


def compiler_result_to_dry_run_dict(result: CompilerResult) -> dict[str, object]:
    """Convert CompilerResult to a dict suitable for dry-run display."""
    output: dict[str, object] = {
        "agent": result.agent_name,
        "model": result.model_token or result.model,
        "canonical_model": result.model,
        "harness": result.harness,
    }

    for field_name in (
        "effort",
        "approval",
        "sandbox",
        "autocompact",
        "autocompact_pct",
        "timeout",
    ):
        value = getattr(result.execution_policy, field_name)
        if value is not None:
            output[field_name] = value

    output["provenance"] = render_provenance(result.field_provenance)

    if result.fallback_chain:
        output["fallback_chain"] = list(result.fallback_chain)
    if result.warnings:
        output["warnings"] = list(result.warnings)
    return output


def compile_launch_params(request: CompilerRequest) -> CompilerResult:
    """Compile launch parameters from layered inputs."""

    warnings: list[str] = []

    model_token, model_source = _resolve_model_token(request)
    canonical_model_id = _canonical_model_id(model_token=model_token, request=request)

    effective_policies, overlay_policy_count = _effective_model_policies(request)
    matched_policy = _match_policy_rule(
        model_policies=effective_policies,
        canonical_model_id=canonical_model_id,
        selected_model_token=model_token,
    )
    policy_source = _matched_policy_provenance(
        matched_policy=matched_policy,
        model_policies=effective_policies,
        overlay_policy_count=overlay_policy_count,
    )
    matched_policy_overrides = _policy_overrides(matched_policy)

    legacy_overrides = RuntimeOverrides()
    if matched_policy is None:
        legacy_overrides, legacy_warning = _resolve_legacy_model_overrides(
            request=request,
            model_token=model_token,
            canonical_model_id=canonical_model_id,
        )
        if legacy_warning:
            warnings.append(legacy_warning)

    policy_override_tier = matched_policy_overrides
    policy_override_source = policy_source if matched_policy is not None else ProvenanceLevel.UNSET
    if matched_policy is None and legacy_overrides.model_dump(exclude_none=True):
        policy_override_tier = legacy_overrides
        policy_override_source = ProvenanceLevel.PROFILE_MODEL_POLICY

    harness, harness_source, harness_provenance = _resolve_harness(
        request=request,
        model_token=model_token,
        model_source=model_source,
        policy_rule=matched_policy,
        policy_source=policy_source,
    )

    alias_defaults = RuntimeOverrides.from_alias_entry(request.resolved_alias_entry)
    overlay_policy_defaults = RuntimeOverrides.from_agent_overlay_policy(request.agent_overlay)

    effort, effort_source = _resolve_execution_policy_field(
        request,
        "effort",
        (request.cli_overrides.effort, ProvenanceLevel.CLI),
        (request.env_overrides.effort, ProvenanceLevel.ENV),
        (policy_override_tier.effort, policy_override_source),
        (overlay_policy_defaults.effort, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),
        (request.profile_policy_defaults.effort, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.effort, ProvenanceLevel.CONFIG_DEFAULT),
        (alias_defaults.effort, ProvenanceLevel.ALIAS_DEFAULT),
    )
    approval, approval_source = _resolve_execution_policy_field(
        request,
        "approval",
        (request.cli_overrides.approval, ProvenanceLevel.CLI),
        (request.env_overrides.approval, ProvenanceLevel.ENV),
        (policy_override_tier.approval, policy_override_source),
        (overlay_policy_defaults.approval, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),
        (request.profile_policy_defaults.approval, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.approval, ProvenanceLevel.CONFIG_DEFAULT),
        (alias_defaults.approval, ProvenanceLevel.ALIAS_DEFAULT),
    )
    sandbox, sandbox_source = _resolve_execution_policy_field(
        request,
        "sandbox",
        (request.cli_overrides.sandbox, ProvenanceLevel.CLI),
        (request.env_overrides.sandbox, ProvenanceLevel.ENV),
        (policy_override_tier.sandbox, policy_override_source),
        (overlay_policy_defaults.sandbox, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),
        (request.profile_policy_defaults.sandbox, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.sandbox, ProvenanceLevel.CONFIG_DEFAULT),
        (alias_defaults.sandbox, ProvenanceLevel.ALIAS_DEFAULT),
    )
    autocompact, autocompact_source = _resolve_execution_policy_field(
        request,
        "autocompact",
        (request.cli_overrides.autocompact, ProvenanceLevel.CLI),
        (request.env_overrides.autocompact, ProvenanceLevel.ENV),
        (policy_override_tier.autocompact, policy_override_source),
        (overlay_policy_defaults.autocompact, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),
        (request.profile_policy_defaults.autocompact, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.autocompact, ProvenanceLevel.CONFIG_DEFAULT),
        (alias_defaults.autocompact, ProvenanceLevel.ALIAS_DEFAULT),
    )
    autocompact_pct, autocompact_pct_source = _resolve_execution_policy_field(
        request,
        "autocompact_pct",
        (request.cli_overrides.autocompact_pct, ProvenanceLevel.CLI),
        (request.env_overrides.autocompact_pct, ProvenanceLevel.ENV),
        (policy_override_tier.autocompact_pct, policy_override_source),
        (overlay_policy_defaults.autocompact_pct, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),
        (request.profile_policy_defaults.autocompact_pct, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.autocompact_pct, ProvenanceLevel.CONFIG_DEFAULT),
        (alias_defaults.autocompact_pct, ProvenanceLevel.ALIAS_DEFAULT),
    )
    timeout, timeout_source = _resolve_execution_policy_field(
        request,
        "timeout",
        (request.cli_overrides.timeout, ProvenanceLevel.CLI),
        (request.env_overrides.timeout, ProvenanceLevel.ENV),
        (
            matched_policy_overrides.timeout,
            policy_source if matched_policy is not None else ProvenanceLevel.UNSET,
        ),
        (None, ProvenanceLevel.AGENT_OVERLAY_DEFAULT),  # Overlay timeout intentionally excluded.
        (
            None,
            ProvenanceLevel.PROFILE_DEFAULT,
        ),  # Profile default timeout not part of v1 profile data.
        (request.config_defaults.timeout, ProvenanceLevel.CONFIG_DEFAULT),
        (None, ProvenanceLevel.ALIAS_DEFAULT),  # Alias timeout defaults are not supported.
    )

    model = canonical_model_id
    requested_token = model_token or model

    return CompilerResult(
        agent_name=request.requested_agent,
        model=model,
        model_token=model_token,
        harness=harness,
        execution_policy=ResolvedExecutionPolicy(
            effort=effort,
            approval=approval,
            sandbox=sandbox,
            autocompact=autocompact,
            autocompact_pct=autocompact_pct,
            timeout=timeout,
        ),
        skill_names=request.profile_skills,
        model_selection_requested_token=requested_token,
        model_selection_canonical_id=canonical_model_id,
        harness_provenance=harness_provenance,
        field_provenance=FieldProvenance(
            model_source=model_source,
            harness_source=harness_source,
            effort_source=effort_source,
            approval_source=approval_source,
            sandbox_source=sandbox_source,
            autocompact_source=autocompact_source,
            autocompact_pct_source=autocompact_pct_source,
            timeout_source=timeout_source,
        ),
        warnings=tuple(warnings),
        model_policy_source=policy_source,
        matched_model_policy=matched_policy is not None,
    )


def match_model_policy(
    *,
    model_policies: tuple[ModelPolicyRule, ...],
    canonical_model_id: str,
    selected_model_token: str,
) -> ModelPolicyRule | None:
    """Find the first matching model policy rule by list order.

    Rules are evaluated in order. The first match wins.
    """

    for rule in model_policies:
        if rule.match_type == "model" and rule.match_value == canonical_model_id:
            return rule
        if rule.match_type == "alias" and rule.match_value == selected_model_token:
            return rule
        if rule.match_type == "model-glob" and fnmatchcase(canonical_model_id, rule.match_value):
            return rule
    return None


def _resolve_field[T](
    *candidates: tuple[T | None, ProvenanceLevel],
) -> tuple[T | None, ProvenanceLevel]:
    for value, source in candidates:
        if value is not None:
            return value, source
    return None, ProvenanceLevel.UNSET


def _resolve_execution_policy_field[T](
    request: CompilerRequest,
    field_name: ExecutionPolicyField,
    *candidates: tuple[T | None, ProvenanceLevel],
) -> tuple[T | None, ProvenanceLevel]:
    if field_name not in set(request.supported_execution_policy_fields):
        return None, ProvenanceLevel.UNSET
    return _resolve_field(*candidates)


def _resolve_model_token(request: CompilerRequest) -> tuple[str, ProvenanceLevel]:
    model, source = _resolve_field(
        (request.cli_overrides.model, ProvenanceLevel.CLI),
        (request.env_overrides.model, ProvenanceLevel.ENV),
        (
            request.agent_overlay.model if request.agent_overlay is not None else None,
            ProvenanceLevel.AGENT_OVERLAY_DEFAULT,
        ),
        (request.profile_routing_model, ProvenanceLevel.PROFILE_DEFAULT),
        (request.config_defaults.model, ProvenanceLevel.CONFIG_DEFAULT),
    )
    return model or "", source


def _canonical_model_id(*, model_token: str, request: CompilerRequest) -> str:
    if not model_token:
        return ""
    if request.resolved_alias_entry is not None:
        return str(request.resolved_alias_entry.model_id)
    return model_token


def _overlay_policy_rules(overlay: AgentOverlayConfig) -> tuple[ModelPolicyRule, ...]:
    if overlay.model_policies is None:
        return ()
    return tuple(
        ModelPolicyRule(
            match_type=cast("Literal['model', 'alias', 'model-glob']", rule.match_type),
            match_value=rule.match_value,
            overrides=dict(rule.overrides),
            no_fallback=rule.no_fallback,
        )
        for rule in overlay.model_policies
    )


def _effective_model_policies(
    request: CompilerRequest,
) -> tuple[tuple[ModelPolicyRule, ...], int]:
    profile_policies = request.profile_model_policies or ()
    if request.agent_overlay is None or request.agent_overlay.model_policies is None:
        return profile_policies, 0

    overlay_policies = _overlay_policy_rules(request.agent_overlay)
    effective_policies = (*overlay_policies, *profile_policies)
    return effective_policies, len(overlay_policies)


def _matched_policy_provenance(
    *,
    matched_policy: ModelPolicyRule | None,
    model_policies: tuple[ModelPolicyRule, ...],
    overlay_policy_count: int,
) -> ProvenanceLevel:
    if matched_policy is None:
        return ProvenanceLevel.UNSET
    for index, candidate in enumerate(model_policies):
        if candidate is matched_policy:
            if index < overlay_policy_count:
                return ProvenanceLevel.AGENT_OVERLAY_POLICY
            return ProvenanceLevel.PROFILE_MODEL_POLICY
    return ProvenanceLevel.UNSET


def _match_policy_rule(
    *,
    model_policies: tuple[ModelPolicyRule, ...],
    canonical_model_id: str,
    selected_model_token: str,
) -> ModelPolicyRule | None:
    if not model_policies or not canonical_model_id or not selected_model_token:
        return None
    return match_model_policy(
        model_policies=model_policies,
        canonical_model_id=canonical_model_id,
        selected_model_token=selected_model_token,
    )


def _policy_overrides(rule: ModelPolicyRule | None) -> RuntimeOverrides:
    if rule is None:
        return RuntimeOverrides()
    return RuntimeOverrides.model_validate(dict(rule.overrides))


def _resolve_legacy_model_overrides(
    *,
    request: CompilerRequest,
    model_token: str,
    canonical_model_id: str,
) -> tuple[RuntimeOverrides, str | None]:
    legacy_models = request.profile_legacy_models or {}
    if not legacy_models or not model_token:
        return RuntimeOverrides(), None

    if model_token in legacy_models:
        return _entry_to_overrides(legacy_models[model_token]), None
    if canonical_model_id and canonical_model_id in legacy_models:
        return _entry_to_overrides(legacy_models[canonical_model_id]), None
    if request.resolved_alias_entry is None:
        return RuntimeOverrides(), None

    selected_model_id = request.resolved_alias_entry.model_id
    matched_keys: list[str] = []
    for key in legacy_models:
        catalog_entry = request.alias_catalog.get(key)
        if catalog_entry is None:
            continue
        if catalog_entry.model_id == selected_model_id:
            matched_keys.append(key)

    if not matched_keys:
        return RuntimeOverrides(), None

    winner = matched_keys[0]
    warning: str | None = None
    if len(matched_keys) > 1:
        warning = (
            f"Profile legacy models has multiple entries matching '{selected_model_id}'. "
            f"Using '{winner}' and ignoring: {', '.join(matched_keys[1:])}."
        )
    return _entry_to_overrides(legacy_models[winner]), warning


def _entry_to_overrides(entry: AgentModelEntry) -> RuntimeOverrides:
    return RuntimeOverrides(
        effort=entry.effort,
        autocompact=entry.autocompact,
        autocompact_pct=entry.autocompact_pct,
    )


def _policy_rule_harness(policy_rule: ModelPolicyRule | None) -> str | None:
    if policy_rule is None:
        return None
    harness = policy_rule.overrides.get("harness")
    if not isinstance(harness, str):
        return None
    normalized = harness.strip()
    return normalized or None


def _model_derived_harness(request: CompilerRequest) -> tuple[str | None, str]:
    if request.resolved_alias_entry is None:
        return None, ""
    entry = request.resolved_alias_entry
    if entry.mars_provided_harness is not None:
        return str(entry.harness), "mars-provided"
    return str(entry.harness), "pattern-fallback"


def _provenance_rank(level: ProvenanceLevel) -> int:
    order = {
        ProvenanceLevel.CLI: 0,
        ProvenanceLevel.ENV: 1,
        ProvenanceLevel.AGENT_OVERLAY_POLICY: 2,
        ProvenanceLevel.AGENT_OVERLAY_DEFAULT: 3,
        ProvenanceLevel.PROFILE_MODEL_POLICY: 4,
        ProvenanceLevel.PROFILE_DEFAULT: 5,
        ProvenanceLevel.CONFIG_DEFAULT: 6,
        ProvenanceLevel.ALIAS_DEFAULT: 7,
        ProvenanceLevel.UNSET: 8,
    }
    return order[level]


def _should_override_harness_with_model(
    *,
    model_token: str,
    model_source: ProvenanceLevel,
    harness_source: ProvenanceLevel,
) -> bool:
    if not model_token or model_source is ProvenanceLevel.UNSET:
        return False
    return _provenance_rank(model_source) < _provenance_rank(harness_source)


def _resolve_harness(
    *,
    request: CompilerRequest,
    model_token: str,
    model_source: ProvenanceLevel,
    policy_rule: ModelPolicyRule | None,
    policy_source: ProvenanceLevel,
) -> tuple[str, ProvenanceLevel, str]:
    policy_harness = _policy_rule_harness(policy_rule)

    harness, harness_source = _resolve_field(
        (request.cli_overrides.harness, ProvenanceLevel.CLI),
        (request.env_overrides.harness, ProvenanceLevel.ENV),
        (
            request.agent_overlay.harness if request.agent_overlay is not None else None,
            ProvenanceLevel.AGENT_OVERLAY_DEFAULT,
        ),
        (policy_harness, policy_source if policy_harness is not None else ProvenanceLevel.UNSET),
        (request.profile_routing_harness, ProvenanceLevel.PROFILE_DEFAULT),
    )
    harness_provenance = "explicit-override" if harness is not None else ""

    if harness is None and model_token:
        model_derived, model_derived_provenance = _model_derived_harness(request)
        if model_derived is not None:
            return model_derived, ProvenanceLevel.ALIAS_DEFAULT, model_derived_provenance

    if harness is None:
        harness = request.config_defaults.harness or request.configured_default_harness or "claude"
        return harness, ProvenanceLevel.CONFIG_DEFAULT, "configured-default"

    if _should_override_harness_with_model(
        model_token=model_token,
        model_source=model_source,
        harness_source=harness_source,
    ):
        model_derived, _model_derived_provenance = _model_derived_harness(request)
        if model_derived is not None and model_derived != harness:
            return model_derived, ProvenanceLevel.ALIAS_DEFAULT, "model-derived-override"

    if harness_source is ProvenanceLevel.AGENT_OVERLAY_POLICY:
        harness_provenance = "agent-overlay-model-policy"
    elif harness_source is ProvenanceLevel.PROFILE_MODEL_POLICY:
        harness_provenance = "profile-model-policy"

    return harness, harness_source, harness_provenance


__all__ = [
    "CompilerRequest",
    "CompilerResult",
    "FieldProvenance",
    "ProvenanceLevel",
    "ResolvedExecutionPolicy",
    "compile_launch_params",
    "compiler_result_to_dry_run_dict",
    "match_model_policy",
    "render_provenance",
]
