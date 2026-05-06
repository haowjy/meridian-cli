"""Policy-resolution stage ownership for launch composition."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from meridian.lib.catalog.agent import AgentModelEntry, AgentProfile
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides, resolve
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry

from .compiler import (
    CompilerRequest,
    CompilerResult,
    ProvenanceLevel,
    compile_launch_params,
    match_model_policy,
)
from .launch_types import CompositionWarning
from .materialize import materialize_harness
from .resolve import (
    ResolvedSkills,
    dedupe_skill_names,
    load_agent_profile_with_fallback,
    resolve_skills_from_profile,
    validate_harness_compatibility,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSelectionContext:
    """Carries model identity and routing context through policy resolution."""

    requested_token: str
    selected_model_token: str
    canonical_model_id: str
    mars_provided_harness: HarnessId | None
    resolved_entry: AliasEntry | None
    harness_provenance: str


@dataclass(frozen=True)
class ResolvedPolicies:
    profile: AgentProfile | None
    model: str
    harness: HarnessId
    adapter: SubprocessHarness
    resolved_skills: ResolvedSkills
    resolved_overrides: RuntimeOverrides
    model_selection: ModelSelectionContext | None = None
    warnings: tuple[CompositionWarning, ...] = ()
    alias_catalog: dict[str, AliasEntry] | None = None


def _resolve_final_model(
    *,
    layer_model: str | None,
    resolved_entry: AliasEntry | None,
    harness_id: HarnessId,
    config: MeridianConfig,
    catalog: CatalogSession,
) -> tuple[str, AliasEntry | None]:
    """Resolve final model string after harness is known."""

    if layer_model:
        if resolved_entry is not None:
            return str(resolved_entry.model_id), resolved_entry
        return layer_model, None

    harness_default = config.default_model_for_harness(str(harness_id))
    fallback_model = harness_default or config.default_model or ""
    if not fallback_model:
        return "", None
    try:
        entry = catalog.resolve_model(fallback_model)
        return str(entry.model_id), entry
    except ValueError:
        return fallback_model, None


def _first_set_layer_index(
    layers: tuple[RuntimeOverrides, ...],
    field_name: str,
) -> int | None:
    for index, layer in enumerate(layers):
        if getattr(layer, field_name) is not None:
            return index
    return None


def _policy_warnings(
    *,
    profile_warning: str | None,
    model_warning: str | None,
) -> tuple[CompositionWarning, ...]:
    normalized_profile_warning = (profile_warning or "").strip()
    normalized_model_warning = (model_warning or "").strip()

    if normalized_profile_warning and normalized_model_warning:
        return (
            CompositionWarning(
                code="policy_warning",
                message=f"{normalized_profile_warning}\n{normalized_model_warning}",
            ),
        )
    if normalized_profile_warning:
        return (
            CompositionWarning(
                code="profile_warning",
                message=normalized_profile_warning,
            ),
        )
    if normalized_model_warning:
        return (
            CompositionWarning(
                code="model_warning",
                message=normalized_model_warning,
            ),
        )
    return ()


def _entry_to_overrides(entry: AgentModelEntry) -> RuntimeOverrides:
    return RuntimeOverrides(
        effort=entry.effort,
        autocompact=entry.autocompact,
    )


def _resolve_profile_model_overrides(
    *,
    profile: AgentProfile | None,
    selected_entry: AliasEntry | None,
    alias_catalog: dict[str, AliasEntry],
) -> tuple[RuntimeOverrides, str | None, bool]:
    if profile is None or not profile.models or selected_entry is None:
        return RuntimeOverrides(), None, False

    selected_alias = selected_entry.alias.strip()
    if selected_alias and selected_alias in profile.models:
        return _entry_to_overrides(profile.models[selected_alias]), None, True

    selected_model_id = str(selected_entry.model_id)
    if selected_model_id in profile.models:
        return _entry_to_overrides(profile.models[selected_model_id]), None, True

    matched_keys: list[str] = []
    for key in profile.models:
        catalog_entry = alias_catalog.get(key)
        if catalog_entry is None:
            continue
        if catalog_entry.model_id == selected_entry.model_id:
            matched_keys.append(key)

    if not matched_keys:
        return RuntimeOverrides(), None, False

    winner = matched_keys[0]
    warning: str | None = None
    if len(matched_keys) > 1:
        warning = (
            f"Agent profile '{profile.name}' has multiple models entries matching "
            f"'{selected_entry.model_id}'. Using '{winner}' and ignoring: "
            f"{', '.join(matched_keys[1:])}."
        )
    return _entry_to_overrides(profile.models[winner]), warning, True


def _harness_is_available(
    harness_id: HarnessId,
    harness_registry: HarnessRegistry,
) -> bool:
    try:
        harness_registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError):
        return False
    return True


def _fallback_entry_for_token(
    token: str,
    *,
    catalog: CatalogSession,
    harness_registry: HarnessRegistry,
) -> tuple[str, HarnessId, AliasEntry | None] | None:
    try:
        entry = catalog.resolve_model(token)
    except ValueError:
        return None
    try:
        harness_id = entry.harness
    except ValueError:
        return None
    if not _harness_is_available(harness_id, harness_registry):
        return None
    return token, harness_id, entry


def _compiler_request_without_model_policies(
    request: CompilerRequest,
) -> CompilerRequest:
    """Return a request variant that suppresses all model-policy sources."""

    overlay = request.agent_overlay
    if overlay is not None:
        overlay = overlay.model_copy(update={"model_policies": ()})
    return replace(
        request,
        agent_overlay=overlay,
        profile_model_policies=(),
    )


def _policy_chain_fallback_candidate(
    *,
    compiler_request: CompilerRequest,
    primary_result: CompilerResult,
    model_explicit: bool,
) -> CompilerResult | None:
    """Return the demoted pre-policy candidate when policy rerouting applies."""

    if model_explicit:
        return None

    stripped_request = _compiler_request_without_model_policies(compiler_request)
    demoted_result = compile_launch_params(stripped_request)
    if (
        demoted_result.model_token == primary_result.model_token
        and demoted_result.model == primary_result.model
        and demoted_result.harness == primary_result.harness
    ):
        return None
    return demoted_result


def _try_harness_availability_fallback(
    *,
    harness_id: HarnessId,
    harness_registry: HarnessRegistry,
    profile: AgentProfile | None,
    model_explicit: bool,
    catalog: CatalogSession,
) -> tuple[str, HarnessId, AliasEntry | None] | None:
    """Attempt fallback when harness is unavailable. Returns None if no fallback found."""

    if model_explicit or _harness_is_available(harness_id, harness_registry) or profile is None:
        return None

    for fanout_entry in profile.fanout:
        fallback_token = fanout_entry.value
        fallback = _fallback_entry_for_token(
            fallback_token,
            catalog=catalog,
            harness_registry=harness_registry,
        )
        if fallback is not None:
            return fallback

    return None


def resolve_policy_fields(
    *tiers: RuntimeOverrides | tuple[RuntimeOverrides, ...],
) -> RuntimeOverrides:
    """Resolve policy-field precedence across an ordered tier ladder.

    Tiers are supplied from highest to lowest precedence, either as positional
    ``RuntimeOverrides`` arguments or as one tuple of ``RuntimeOverrides``.
    Routing fields should be stripped by callers before resolution when the
    policy scope excludes them.
    """

    resolved_tiers: tuple[RuntimeOverrides, ...]
    if len(tiers) == 1 and isinstance(tiers[0], tuple):
        resolved_tiers = tiers[0]
    else:
        resolved_tiers = tuple(_require_policy_tier(tier) for tier in tiers)
    return resolve(*resolved_tiers)


def _require_policy_tier(
    tier: RuntimeOverrides | tuple[RuntimeOverrides, ...],
) -> RuntimeOverrides:
    if isinstance(tier, tuple):
        raise TypeError(
            "resolve_policy_fields() accepts a tier tuple only as its sole argument."
        )
    return tier


def _resolve_model_policy_overrides(
    *,
    explicit_user_overrides: RuntimeOverrides,
    profile_model_overrides: RuntimeOverrides,
    profile_defaults: RuntimeOverrides,
    config_overrides: RuntimeOverrides,
    alias_defaults: RuntimeOverrides,
) -> RuntimeOverrides:
    """Deprecated compatibility wrapper for the legacy five-tier policy helper."""

    return resolve_policy_fields(
        explicit_user_overrides,
        profile_model_overrides,
        profile_defaults,
        config_overrides,
        alias_defaults,
    )


def _log_unmatched_profile_policy_defaults(
    *,
    profile: AgentProfile | None,
    selected_entry: AliasEntry | None,
    model_entry_matched: bool,
    profile_defaults: RuntimeOverrides,
) -> None:
    if profile is None or not profile.models or selected_entry is None or model_entry_matched:
        return
    if not profile_defaults.model_dump(exclude_none=True):
        return
    _LOGGER.debug(
        "Agent profile '%s' has generic model-policy defaults but no matching "
        "models entry for '%s'; using generic profile defaults.",
        profile.name,
        selected_entry.model_id,
    )


def _model_entry_harness_provenance(entry: AliasEntry) -> str:
    if entry.mars_provided_harness is not None:
        return "mars-provided"
    return "pattern-fallback"


def resolve_harness_routing(
    *,
    resolved: RuntimeOverrides,
    resolved_entry: AliasEntry | None,
    model_resolution_error: ValueError | None,
    policy_rule_harness: str | None,
    model_layer_index: int | None,
    harness_layer_index: int | None,
    pre_profile_layer_count: int,
    configured_default_harness: str,
) -> tuple[HarnessId, str | None]:
    """Resolve harness from model identity and override layers.

    Returns (harness_id, provenance_note).
    """

    explicit_harness = (
        resolved.harness
        if harness_layer_index is not None and harness_layer_index < pre_profile_layer_count
        else None
    )

    if explicit_harness:
        harness_id = HarnessId(explicit_harness)
        provenance_note = "explicit-override"
    elif policy_rule_harness:
        harness_id = HarnessId(policy_rule_harness)
        provenance_note = "profile-model-policy"
    elif resolved.harness:
        harness_id = HarnessId(resolved.harness)
        provenance_note = "explicit-override"
    elif resolved.model:
        if model_resolution_error is not None:
            raise model_resolution_error
        assert resolved_entry is not None
        harness_id = resolved_entry.harness
        provenance_note = _model_entry_harness_provenance(resolved_entry)
    else:
        harness_id = HarnessId(configured_default_harness or "claude")
        provenance_note = "configured-default"

    model_set_in_pre_profile_layers = (
        model_layer_index is not None and model_layer_index < pre_profile_layer_count
    )
    harness_from_profile_or_config = (
        harness_layer_index is not None and harness_layer_index >= pre_profile_layer_count
    )
    harness_from_model_policy = policy_rule_harness is not None
    if (
        resolved.model
        and model_set_in_pre_profile_layers
        and (harness_from_model_policy or harness_from_profile_or_config)
    ):
        if model_resolution_error is not None:
            raise model_resolution_error
        assert resolved_entry is not None
        model_derived_harness = resolved_entry.harness
        if harness_id != model_derived_harness:
            harness_id = model_derived_harness
            provenance_note = "model-derived-override"

    return harness_id, provenance_note


def resolve_policies(
    *,
    catalog: CatalogSession,
    layers: tuple[RuntimeOverrides, ...],
    config_overrides: RuntimeOverrides,
    config: MeridianConfig,
    harness_registry: HarnessRegistry,
    configured_default_harness: str = "claude",
    skills_readonly: bool = True,
) -> ResolvedPolicies:
    """Resolve launch policies (model/harness/skills/profile) for one request."""

    project_root = catalog.project_root
    pre_profile_resolved = resolve(*layers, config_overrides)
    explicit_user_overrides = resolve(*layers)
    requested_agent = explicit_user_overrides.agent
    configured_default_agent = pre_profile_resolved.agent if not requested_agent else ""

    profile, profile_warning = load_agent_profile_with_fallback(
        project_root=project_root,
        requested_agent=requested_agent,
        configured_default=configured_default_agent,
    )

    profile_overrides = RuntimeOverrides.from_agent_profile(profile)
    full_layers = (*layers, profile_overrides, config_overrides)
    base_resolved = resolve(*full_layers)

    model_layer_index = _first_set_layer_index(full_layers, "model")
    harness_layer_index = _first_set_layer_index(full_layers, "harness")
    pre_profile_layer_count = len(layers)
    model_explicit = (
        model_layer_index is not None and model_layer_index < pre_profile_layer_count
    )
    user_explicit_same_precedence = (
        model_layer_index is not None
        and harness_layer_index is not None
        and model_layer_index == harness_layer_index
        and model_layer_index < pre_profile_layer_count
    )

    selected_agent_name = profile.name if profile is not None else (
        requested_agent or configured_default_agent or ""
    )
    agent_overlay = config.agents.get(selected_agent_name) if selected_agent_name else None

    requested_model_token = (
        explicit_user_overrides.model
        or (agent_overlay.model if agent_overlay is not None else None)
        or (profile.model if profile is not None else None)
        or config_overrides.model
        or ""
    )
    resolved_entry: AliasEntry | None = None
    model_resolution_error: ValueError | None = None
    if requested_model_token:
        try:
            resolved_entry = catalog.resolve_model(requested_model_token)
        except ValueError as exc:
            model_resolution_error = exc

    alias_catalog = catalog.alias_map()
    profile_skills = dedupe_skill_names(profile.skills) if profile is not None else ()
    compiler_request = CompilerRequest(
        requested_agent=requested_agent,
        requested_model=explicit_user_overrides.model,
        cli_overrides=explicit_user_overrides,
        env_overrides=RuntimeOverrides(),
        agent_overlay=agent_overlay,
        config_defaults=config_overrides,
        profile_routing_model=profile.model if profile is not None else None,
        profile_routing_harness=profile.harness if profile is not None else None,
        profile_policy_effort=profile.effort if profile is not None else None,
        profile_policy_approval=profile.approval if profile is not None else None,
        profile_policy_sandbox=profile.sandbox if profile is not None else None,
        profile_policy_autocompact=profile.autocompact if profile is not None else None,
        profile_model_policies=profile.model_policies if profile is not None else None,
        profile_legacy_models=dict(profile.models) if profile is not None else None,
        profile_fanout=profile.fanout if profile is not None else None,
        profile_skills=profile_skills,
        resolved_alias_entry=resolved_entry,
        alias_catalog=alias_catalog,
        configured_default_harness=configured_default_harness,
        project_root=project_root.as_posix(),
    )

    compiler_result = compile_launch_params(compiler_request)
    requested_token_for_selection = compiler_result.model_selection_requested_token
    harness_id = HarnessId(compiler_result.harness)
    harness_provenance = compiler_result.harness_provenance

    materialized = None
    try:
        materialized = materialize_harness(compiler_result, harness_registry=harness_registry)
    except ValueError as primary_error:
        demoted_candidate = _policy_chain_fallback_candidate(
            compiler_request=compiler_request,
            primary_result=compiler_result,
            model_explicit=model_explicit,
        )
        if demoted_candidate is not None:
            try:
                compiler_result = demoted_candidate
                harness_id = HarnessId(compiler_result.harness)
                harness_provenance = "availability-fallback"
                model_resolution_error = None
                materialized = materialize_harness(
                    compiler_result,
                    harness_registry=harness_registry,
                )
            except ValueError:
                demoted_candidate = None

        if demoted_candidate is None:
            fallback = _try_harness_availability_fallback(
                harness_id=harness_id,
                harness_registry=harness_registry,
                profile=profile,
                model_explicit=model_explicit,
                catalog=catalog,
            )
            if fallback is None:
                raise primary_error
            fallback_model, harness_id, resolved_entry = fallback
            compiler_request = replace(
                compiler_request,
                cli_overrides=explicit_user_overrides.model_copy(update={"model": fallback_model}),
                resolved_alias_entry=resolved_entry,
            )
            # Fanout entries participate as raw availability-chain candidates.
            # Once selected, recompile without model-policies to avoid
            # secondary policy matches leaking scalar overrides into fallback.
            compiler_result = compile_launch_params(
                _compiler_request_without_model_policies(compiler_request)
            )
            if compiler_result.harness != str(harness_id):
                compiler_result = replace(compiler_result, harness=str(harness_id))
            model_resolution_error = None
            harness_provenance = "availability-fallback"
            materialized = materialize_harness(
                compiler_result,
                harness_registry=harness_registry,
            )
    assert materialized is not None

    model_token = compiler_result.model_token
    if model_token and resolved_entry is None and model_resolution_error is None:
        try:
            resolved_entry = catalog.resolve_model(model_token)
        except ValueError:
            resolved_entry = None

    # If model resolution failed but harness is explicit, bind the raw
    # model string to the explicit harness instead of failing.
    if (
        base_resolved.harness
        and not user_explicit_same_precedence
        and model_resolution_error is not None
        and resolved_entry is None
        and model_token
    ):
        resolved_entry = AliasEntry(
            alias="",
            model_id=ModelId(model_token),
            resolved_harness=harness_id,
        )
        model_resolution_error = None

    final_model, resolved_model_entry = _resolve_final_model(
        layer_model=model_token,
        resolved_entry=resolved_entry,
        harness_id=harness_id,
        config=config,
        catalog=catalog,
    )

    if final_model and user_explicit_same_precedence:
        if model_resolution_error is not None:
            raise model_resolution_error
        validate_harness_compatibility(
            model=final_model,
            harness_id=harness_id,
            model_entry=resolved_model_entry,
            harness_registry=harness_registry,
            is_policy_reroute=False,
        )

    selected_entry: AliasEntry | None = resolved_model_entry
    model_selection: ModelSelectionContext | None = None
    if final_model:
        selected_model_token = (
            (selected_entry.alias.strip() or str(selected_entry.model_id))
            if selected_entry is not None
            else final_model
        )
        model_selection = ModelSelectionContext(
            requested_token=requested_token_for_selection or final_model,
            selected_model_token=selected_model_token,
            canonical_model_id=(
                str(selected_entry.model_id) if selected_entry is not None else final_model
            ),
            mars_provided_harness=(
                selected_entry.mars_provided_harness if selected_entry is not None else None
            ),
            resolved_entry=selected_entry,
            harness_provenance=harness_provenance or "",
        )

    profile_policy_rule_matched = compiler_result.matched_model_policy and (
        compiler_result.model_policy_source is ProvenanceLevel.PROFILE_MODEL_POLICY
    )
    model_entry_matched = profile_policy_rule_matched
    if not model_entry_matched:
        _, _, model_entry_matched = _resolve_profile_model_overrides(
            profile=profile,
            selected_entry=selected_entry,
            alias_catalog=alias_catalog,
        )
    profile_policy_defaults = profile_overrides.model_policy_scope()
    if (
        profile is not None
        and profile.model_policies
        and compiler_result.model_policy_source is ProvenanceLevel.PROFILE_MODEL_POLICY
        and not profile_policy_rule_matched
        and selected_entry is not None
        and profile_policy_defaults.model_dump(exclude_none=True)
    ):
        _LOGGER.debug(
            "No model-policies rule matched for '%s'; using generic profile "
            "model-policy defaults.",
            selected_entry.model_id,
        )
    _log_unmatched_profile_policy_defaults(
        profile=profile,
        selected_entry=selected_entry,
        model_entry_matched=model_entry_matched,
        profile_defaults=profile_policy_defaults,
    )

    resolved = base_resolved.model_copy(
        update=RuntimeOverrides(
            effort=compiler_result.effort,
            approval=compiler_result.approval,
            sandbox=compiler_result.sandbox,
            autocompact=compiler_result.autocompact,
            timeout=compiler_result.timeout,
        ).model_dump(exclude_none=True)
    )

    skill_selected_model_token = (
        model_selection.selected_model_token if model_selection is not None else final_model
    )
    skill_canonical_model_id = (
        model_selection.canonical_model_id if model_selection is not None else final_model
    )
    resolved_skills = resolve_skills_from_profile(
        profile_skills=profile_skills,
        project_root=project_root,
        readonly=skills_readonly,
        harness_id=str(harness_id),
        selected_model_token=skill_selected_model_token,
        canonical_model_id=skill_canonical_model_id,
    )

    model_warning = "\n".join(compiler_result.warnings).strip() or None

    return ResolvedPolicies(
        profile=profile,
        model=final_model,
        harness=harness_id,
        adapter=materialized.adapter,
        resolved_skills=resolved_skills,
        resolved_overrides=resolved,
        model_selection=model_selection,
        warnings=_policy_warnings(
            profile_warning=profile_warning,
            model_warning=model_warning,
        ),
        alias_catalog=alias_catalog,
    )


__all__ = [
    "ModelSelectionContext",
    "ResolvedPolicies",
    "_resolve_model_policy_overrides",
    "match_model_policy",
    "resolve_harness_routing",
    "resolve_policies",
    "resolve_policy_fields",
    "validate_harness_compatibility",
]
