"""Policy-resolution stage ownership for launch composition."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from fnmatch import fnmatchcase

from meridian.lib.catalog.agent import AgentModelEntry, AgentProfile, ModelPolicyRule
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides, resolve
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry

from .launch_types import CompositionWarning
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


def _policy_rule_to_overrides(rule: ModelPolicyRule) -> RuntimeOverrides:
    return RuntimeOverrides.model_validate(dict(rule.overrides))


def match_model_policy(
    *,
    model_policies: tuple[ModelPolicyRule, ...],
    canonical_model_id: str,
    selected_model_token: str,
) -> ModelPolicyRule | None:
    """Find the single best-matching model policy rule.

    Ranking: exact model > exact alias > model-glob. Ambiguity at the
    same specificity rank raises ValueError.
    """

    ranked_matches: list[tuple[int, ModelPolicyRule]] = []
    for rule in model_policies:
        if rule.match_type == "model" and rule.match_value == canonical_model_id:
            ranked_matches.append((0, rule))
        elif rule.match_type == "alias" and rule.match_value == selected_model_token:
            ranked_matches.append((1, rule))
        elif rule.match_type == "model-glob" and fnmatchcase(canonical_model_id, rule.match_value):
            ranked_matches.append((2, rule))

    if not ranked_matches:
        return None

    best_rank = min(rank for rank, _rule in ranked_matches)
    winners = [rule for rank, rule in ranked_matches if rank == best_rank]
    if len(winners) > 1:
        match_kind = {
            0: "model",
            1: "alias",
            2: "model-glob",
        }[best_rank]
        values = ", ".join(rule.match_value for rule in winners)
        raise ValueError(
            f"Ambiguous model-policies for {match_kind} match on "
            f"'{selected_model_token}' / '{canonical_model_id}': {values}."
        )
    return winners[0]


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

    for rule in profile.model_policies:
        if rule.match_type == "model-glob":
            continue
        fallback = _fallback_entry_for_token(
            rule.match_value,
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
    requested_agent = resolve(*layers).agent
    configured_default_agent = pre_profile_resolved.agent if not requested_agent else ""

    profile, profile_warning = load_agent_profile_with_fallback(
        project_root=project_root,
        requested_agent=requested_agent,
        configured_default=configured_default_agent,
    )
    profile_overrides = RuntimeOverrides.from_agent_profile(profile)
    full_layers = (*layers, profile_overrides, config_overrides)
    resolved = resolve(*full_layers)
    resolved_entry: AliasEntry | None = None
    model_resolution_error: ValueError | None = None
    if resolved.model:
        try:
            resolved_entry = catalog.resolve_model(resolved.model)
        except ValueError as exc:
            model_resolution_error = exc

    matched_policy_rule = (
        match_model_policy(
            model_policies=profile.model_policies,
            canonical_model_id=str(resolved_entry.model_id),
            selected_model_token=resolved.model,
        )
        if profile is not None and resolved_entry is not None and resolved.model is not None
        else None
    )
    policy_rule_harness = (
        str(matched_policy_rule.overrides["harness"]).strip()
        if matched_policy_rule is not None and matched_policy_rule.overrides.get("harness")
        else None
    )

    model_layer_index = _first_set_layer_index(full_layers, "model")
    harness_layer_index = _first_set_layer_index(full_layers, "harness")
    pre_profile_layer_count = len(layers)

    harness_id, harness_provenance = resolve_harness_routing(
        resolved=resolved,
        resolved_entry=resolved_entry,
        model_resolution_error=model_resolution_error,
        policy_rule_harness=policy_rule_harness,
        model_layer_index=model_layer_index,
        harness_layer_index=harness_layer_index,
        pre_profile_layer_count=pre_profile_layer_count,
        configured_default_harness=configured_default_harness,
    )
    model_explicit = (
        model_layer_index is not None and model_layer_index < pre_profile_layer_count
    )
    original_requested_token = resolved.model or ""
    fallback = _try_harness_availability_fallback(
        harness_id=harness_id,
        harness_registry=harness_registry,
        profile=profile,
        model_explicit=model_explicit,
        catalog=catalog,
    )
    if fallback is not None:
        fallback_model, harness_id, resolved_entry = fallback
        resolved = resolved.model_copy(update={"model": fallback_model})
        model_resolution_error = None
        harness_provenance = "availability-fallback"
    user_explicit_same_precedence = (
        model_layer_index is not None
        and harness_layer_index is not None
        and model_layer_index == harness_layer_index
        and model_layer_index < pre_profile_layer_count
    )
    # If model resolution failed but harness is explicit, bind the raw
    # model string to the explicit harness instead of failing.
    if (
        resolved.harness
        and not user_explicit_same_precedence
        and model_resolution_error is not None
        and resolved_entry is None
        and resolved.model is not None
    ):
        resolved_entry = AliasEntry(
            alias="",
            model_id=ModelId(resolved.model),
            resolved_harness=harness_id,
        )
        model_resolution_error = None

    try:
        adapter = harness_registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError) as exc:
        supported = ", ".join(str(harness) for harness in harness_registry.ids())
        raise ValueError(
            f"Unknown or unsupported harness '{harness_id}'. Available harnesses: {supported}"
        ) from exc

    final_model, resolved_model_entry = _resolve_final_model(
        layer_model=resolved.model,
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
            requested_token=original_requested_token or final_model,
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

    alias_catalog = catalog.alias_map()
    selected_model_token = (
        model_selection.selected_model_token if model_selection is not None else ""
    )
    matched_policy_rule = (
        match_model_policy(
            model_policies=profile.model_policies,
            canonical_model_id=str(selected_entry.model_id),
            selected_model_token=selected_model_token,
        )
        if profile is not None and selected_entry is not None
        else None
    )
    if matched_policy_rule is not None:
        profile_model_overrides = _policy_rule_to_overrides(matched_policy_rule)
        model_warning = None
        model_entry_matched = True
    else:
        (
            profile_model_overrides,
            model_warning,
            model_entry_matched,
        ) = _resolve_profile_model_overrides(
            profile=profile,
            selected_entry=selected_entry,
            alias_catalog=alias_catalog,
        )
    alias_defaults = RuntimeOverrides.from_alias_entry(selected_entry)
    profile_policy_defaults = profile_overrides.model_policy_scope()
    if (
        profile is not None
        and profile.model_policies
        and matched_policy_rule is None
        and selected_entry is not None
        and profile_policy_defaults.model_dump(exclude_none=True)
    ):
        _LOGGER.debug(
            "No model-policies rule matched for '%s'; using generic profile "
            "model-policy defaults.",
            selected_entry.model_id,
        )
    explicit_user_overrides = resolve(*layers).model_policy_scope()
    model_policy_resolved = resolve_policy_fields(
        explicit_user_overrides,
        profile_model_overrides.model_policy_scope(),
        profile_policy_defaults,
        config_overrides.model_policy_scope(),
        alias_defaults.model_policy_scope(),
    )
    _log_unmatched_profile_policy_defaults(
        profile=profile,
        selected_entry=selected_entry,
        model_entry_matched=model_entry_matched,
        profile_defaults=profile_policy_defaults,
    )
    resolved = resolved.model_copy(
        update=model_policy_resolved.model_dump(exclude_none=True)
    )

    profile_skills: tuple[str, ...] = ()
    if profile is not None:
        profile_skills = dedupe_skill_names(profile.skills)
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

    return ResolvedPolicies(
        profile=profile,
        model=final_model,
        harness=harness_id,
        adapter=adapter,
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
