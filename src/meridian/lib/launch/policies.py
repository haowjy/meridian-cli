"""Policy-resolution stage ownership for launch composition."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from meridian.lib.catalog.agent import AgentProfile, ModelPolicyRule
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import (
    EXECUTION_POLICY_FIELDS,
    ExecutionPolicyField,
    RuntimeOverrides,
    normalize_execution_policy_fields,
    resolve,
)
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry

from .compiler import (
    CompilerRequest,
    CompilerResult,
    FieldProvenance,
    ProvenanceLevel,
    compile_launch_params,
    effective_model_policies,
    match_model_policy,
)
from .launch_types import (
    CompositionWarning,
    ResolvedExecutionPolicy,
    ResolvedLaunchRouting,
    TerminalSurfaceMode,
)
from .materialize import materialize_harness
from .request import LaunchCompositionSurface
from .resolve import (
    ResolvedSkills,
    dedupe_skill_names,
    load_agent_profile_with_fallback,
    resolve_skills_from_profile,
    select_harness_model_id,
    validate_harness_compatibility,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_EXECUTION_POLICY_FIELDS: frozenset[ExecutionPolicyField] = frozenset(
    EXECUTION_POLICY_FIELDS
)


@dataclass(frozen=True)
class ModelSelectionContext:
    """Carries model identity and routing context through policy resolution."""

    requested_token: str
    selected_model_token: str
    canonical_model_id: str
    mars_provided_harness: HarnessId | None
    resolved_entry: AliasEntry | None
    harness_provenance: str
    harness_model_id: str | None = None


@dataclass(frozen=True)
class SurfacePolicyInput:
    """Surface-neutral input for shared launch policy resolution."""

    surface: LaunchCompositionSurface
    catalog: CatalogSession
    layers: tuple[RuntimeOverrides, ...]
    config_overrides: RuntimeOverrides
    config: MeridianConfig
    harness_registry: HarnessRegistry
    configured_default_harness: str = "claude"
    skills_readonly: bool = True
    requested_skills: tuple[str, ...] = ()
    supported_execution_policy_fields: frozenset[ExecutionPolicyField] = (
        _DEFAULT_EXECUTION_POLICY_FIELDS
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported_execution_policy_fields",
            frozenset(normalize_execution_policy_fields(self.supported_execution_policy_fields)),
        )

    @property
    def cli_overrides(self) -> RuntimeOverrides:
        return self.layers[0] if self.layers else RuntimeOverrides()

    @property
    def env_overrides(self) -> RuntimeOverrides:
        return self.layers[1] if len(self.layers) > 1 else RuntimeOverrides()

    @property
    def routing_layers(self) -> tuple[RuntimeOverrides, ...]:
        return tuple(layer.routing_scope() for layer in (*self.layers, self.config_overrides))

    @property
    def execution_policy_layers(self) -> tuple[RuntimeOverrides, ...]:
        return tuple(
            layer.execution_policy_scope(self.supported_execution_policy_fields)
            for layer in (*self.layers, self.config_overrides)
        )


@dataclass(frozen=True)
class ResolvedLaunchPolicy:
    """Resolved launch policy shared across launch-like surfaces."""

    profile: AgentProfile | None
    model: str
    harness: HarnessId
    adapter: SubprocessHarness
    resolved_skills: ResolvedSkills
    routing: ResolvedLaunchRouting
    execution_policy: ResolvedExecutionPolicy
    terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED
    field_provenance: FieldProvenance = field(default_factory=FieldProvenance)
    model_selection: ModelSelectionContext | None = None
    fallback_chain: tuple[dict[str, object], ...] = ()
    warnings: tuple[CompositionWarning, ...] = ()
    alias_catalog: dict[str, AliasEntry] | None = None


ResolvedPolicies = ResolvedLaunchPolicy


def _resolve_terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
    """Resolve the interactive terminal surface mode at the policy boundary.

    Stage 3.4 lands the typed policy field without changing interactive launch
    behavior. The compatibility default remains PTY-mediated for every harness.
    Claude is permanently pinned to PTY-mediated; Codex/OpenCode also remain on
    PTY-mediated until later rollout gates enable native inherit.
    """

    _ = harness_id
    return TerminalSurfaceMode.PTY_MEDIATED


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


def _is_pre_profile_explicit_layer(
    *,
    layer_index: int | None,
    pre_profile_layer_count: int,
) -> bool:
    return layer_index is not None and layer_index < pre_profile_layer_count


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


def _resolved_model_default_harness(model_entry: AliasEntry | None) -> HarnessId | None:
    """Return a model entry's default harness when it can be resolved."""

    if model_entry is None:
        return None
    try:
        return model_entry.harness
    except ValueError:
        return None


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


def _compiler_request_for_base_candidate(
    request: CompilerRequest,
) -> CompilerRequest:
    """Return request data for the untransformed base launch candidate."""

    overlay = request.agent_overlay
    if overlay is not None:
        overlay = overlay.model_copy(update={"model_policies": ()})
    return replace(
        request,
        agent_overlay=overlay,
        profile_model_policies=(),
    )


def _compiler_request_for_fallback_candidate(
    request: CompilerRequest,
    matched_rule: ModelPolicyRule,
) -> CompilerRequest:
    """Return fallback-candidate request while preserving matched-rule overrides.

    Fallback candidates must not recursively re-run model-policies against the
    fallback token, but they must carry the selected matched rule's policy
    overrides so the compiled fallback keeps its intended execution policy and
    harness routing.
    """

    policy_overrides = RuntimeOverrides.model_validate(dict(matched_rule.overrides))
    preserved_fields: tuple[str, ...] = ("harness", *EXECUTION_POLICY_FIELDS)
    injected_fields = {
        field_name: value
        for field_name in preserved_fields
        if (value := getattr(policy_overrides, field_name)) is not None
        and getattr(request.cli_overrides, field_name) is None
        and getattr(request.env_overrides, field_name) is None
    }
    merged_cli = request.cli_overrides.model_copy(update=injected_fields)
    overlay = request.agent_overlay
    if overlay is not None:
        overlay = overlay.model_copy(update={"model_policies": ()})
    return replace(
        request,
        cli_overrides=merged_cli,
        agent_overlay=overlay,
        profile_model_policies=(),
    )


def _build_final_resolved_views(
    *,
    base_resolved: RuntimeOverrides,
    compiler_result: CompilerResult,
    harness_id: HarnessId,
    supported_execution_policy_fields: frozenset[ExecutionPolicyField],
) -> tuple[ResolvedLaunchRouting, ResolvedExecutionPolicy]:
    resolved_routing = ResolvedLaunchRouting(
        model=compiler_result.model_token or None,
        harness=harness_id,
        agent=base_resolved.agent,
    )
    scoped_overrides = compiler_result.execution_policy.as_overrides(
        supported_fields=supported_execution_policy_fields,
    )
    resolved_execution_policy = ResolvedExecutionPolicy(
        effort=scoped_overrides.effort,
        sandbox=scoped_overrides.sandbox,
        approval=scoped_overrides.approval,
        autocompact=scoped_overrides.autocompact,
        autocompact_pct=scoped_overrides.autocompact_pct,
        timeout=scoped_overrides.timeout,
    )
    return resolved_routing, resolved_execution_policy


def _demoted_base_candidate(
    *,
    compiler_request: CompilerRequest,
    primary_result: CompilerResult,
    model_explicit: bool,
) -> CompilerResult | None:
    """Return the demoted base candidate when policy transforms apply."""

    if model_explicit or not primary_result.matched_model_policy:
        return None

    stripped_request = _compiler_request_for_base_candidate(compiler_request)
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
    model_policies: tuple[ModelPolicyRule, ...],
    model_explicit: bool,
    catalog: CatalogSession,
) -> tuple[str, HarnessId, AliasEntry | None, ModelPolicyRule] | None:
    """Attempt fallback when harness is unavailable. Returns None if no fallback found."""

    if model_explicit or _harness_is_available(harness_id, harness_registry) or not model_policies:
        return None

    for fallback_token, fallback_harness, fallback_entry, matched_rule in (
        _fallback_candidates_from_policies(
            model_policies=model_policies,
            catalog=catalog,
            harness_registry=harness_registry,
        )
    ):
        return fallback_token, fallback_harness, fallback_entry, matched_rule

    return None


def _fallback_candidates_from_policies(
    *,
    model_policies: tuple[ModelPolicyRule, ...],
    catalog: CatalogSession,
    harness_registry: HarnessRegistry,
) -> list[tuple[str, HarnessId, AliasEntry | None, ModelPolicyRule]]:
    """Build ordered harness-availability candidates from effective model-policies."""

    candidates: list[tuple[str, HarnessId, AliasEntry | None, ModelPolicyRule]] = []
    for rule in model_policies:
        if rule.no_fallback or rule.match_type == "model-glob":
            continue
        fallback = _fallback_entry_for_token(
            rule.match_value,
            catalog=catalog,
            harness_registry=harness_registry,
        )
        if fallback is None:
            continue
        token, fallback_harness, entry = fallback
        candidates.append((token, fallback_harness, entry, rule))
    return candidates


def _effective_fallback_chain(
    model_policies: tuple[ModelPolicyRule, ...],
) -> tuple[dict[str, object], ...]:
    fallback_chain: list[dict[str, object]] = []
    for position, rule in enumerate(model_policies, start=1):
        if rule.no_fallback or rule.match_type not in {"alias", "model"}:
            continue
        fallback_chain.append(
            {
                "token": rule.match_value,
                "position": position,
                "override_summary": {key: rule.overrides[key] for key in sorted(rule.overrides)},
            }
        )
    return tuple(fallback_chain)


def _with_fallback_chain(
    compiler_result: CompilerResult,
    model_policies: tuple[ModelPolicyRule, ...],
) -> CompilerResult:
    fallback_chain = _effective_fallback_chain(model_policies)
    if not fallback_chain:
        return compiler_result
    return replace(compiler_result, fallback_chain=fallback_chain)


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
        raise TypeError("resolve_policy_fields() accepts a tier tuple only as its sole argument.")
    return tier


def resolve_launch_policy(surface: SurfacePolicyInput) -> ResolvedLaunchPolicy:
    """Resolve the shared launch policy boundary for one launch-like surface."""

    project_root = surface.catalog.project_root
    pre_profile_resolved = resolve(*surface.layers, surface.config_overrides)
    explicit_user_overrides = resolve(*surface.layers)
    requested_agent = explicit_user_overrides.agent
    configured_default_agent = pre_profile_resolved.agent if not requested_agent else ""

    profile, profile_warning = load_agent_profile_with_fallback(
        project_root=project_root,
        requested_agent=requested_agent,
        configured_default=configured_default_agent,
    )

    profile_overrides = RuntimeOverrides.from_agent_profile(profile)
    full_layers = (*surface.layers, profile_overrides, surface.config_overrides)
    base_resolved = resolve(*full_layers)

    model_layer_index = _first_set_layer_index(full_layers, "model")
    harness_layer_index = _first_set_layer_index(full_layers, "harness")
    pre_profile_layer_count = len(surface.layers)
    model_explicit = model_layer_index is not None and model_layer_index < pre_profile_layer_count
    user_explicit_same_precedence = (
        model_layer_index is not None
        and harness_layer_index is not None
        and model_layer_index == harness_layer_index
        and model_layer_index < pre_profile_layer_count
    )

    selected_agent_name = (
        profile.name if profile is not None else (requested_agent or configured_default_agent or "")
    )
    agent_overlay = surface.config.agents.get(selected_agent_name) if selected_agent_name else None
    effective_policies, _overlay_policy_count = effective_model_policies(
        profile_model_policies=profile.model_policies if profile is not None else None,
        agent_overlay=agent_overlay,
    )

    requested_model_token = (
        explicit_user_overrides.model
        or (agent_overlay.model if agent_overlay is not None else None)
        or (profile.model if profile is not None else None)
        or surface.config_overrides.model
        or ""
    )
    resolved_entry: AliasEntry | None = None
    model_resolution_error: ValueError | None = None
    if requested_model_token:
        try:
            resolved_entry = surface.catalog.resolve_model(requested_model_token)
        except ValueError as exc:
            model_resolution_error = exc

    alias_catalog: dict[str, AliasEntry] = {}
    if requested_model_token:
        alias_catalog = surface.catalog.alias_map()
    profile_skills = dedupe_skill_names(profile.skills) if profile is not None else ()
    compiler_request = CompilerRequest(
        requested_agent=requested_agent,
        cli_overrides=surface.cli_overrides,
        env_overrides=surface.env_overrides,
        agent_overlay=agent_overlay,
        config_defaults=surface.config_overrides.execution_policy_scope(
            surface.supported_execution_policy_fields
        ).model_copy(update=surface.config_overrides.routing_scope().model_dump(exclude_none=True)),
        profile_routing_model=profile.model if profile is not None else None,
        profile_routing_harness=profile.harness if profile is not None else None,
        profile_policy_defaults=ResolvedExecutionPolicy(
            effort=profile.effort if profile is not None else None,
            approval=profile.approval if profile is not None else None,
            sandbox=profile.sandbox if profile is not None else None,
            autocompact=profile.autocompact if profile is not None else None,
            autocompact_pct=profile.autocompact_pct if profile is not None else None,
        ),
        profile_model_policies=profile.model_policies if profile is not None else None,
        profile_skills=profile_skills,
        resolved_alias_entry=resolved_entry,
        alias_catalog=alias_catalog,
        configured_default_harness=surface.configured_default_harness,
        project_root=project_root.as_posix(),
        supported_execution_policy_fields=tuple(surface.supported_execution_policy_fields),
    )

    compiler_result = _with_fallback_chain(
        compile_launch_params(compiler_request),
        effective_policies,
    )
    requested_token_for_selection = compiler_result.model_selection_requested_token
    harness_id = HarnessId(compiler_result.harness)
    harness_provenance = compiler_result.harness_provenance

    materialized = None
    try:
        materialized = materialize_harness(
            compiler_result, harness_registry=surface.harness_registry
        )
    except ValueError as primary_error:
        demoted_candidate = _demoted_base_candidate(
            compiler_request=compiler_request,
            primary_result=compiler_result,
            model_explicit=model_explicit,
        )
        if demoted_candidate is not None:
            try:
                compiler_result = demoted_candidate
                compiler_result = _with_fallback_chain(compiler_result, effective_policies)
                harness_id = HarnessId(compiler_result.harness)
                harness_provenance = "availability-fallback"
                model_resolution_error = None
                materialized = materialize_harness(
                    compiler_result,
                    harness_registry=surface.harness_registry,
                )
            except ValueError:
                demoted_candidate = None

        if demoted_candidate is None:
            fallback = _try_harness_availability_fallback(
                harness_id=harness_id,
                harness_registry=surface.harness_registry,
                model_policies=effective_policies,
                model_explicit=model_explicit,
                catalog=surface.catalog,
            )
            if fallback is None:
                raise primary_error
            fallback_model, harness_id, resolved_entry, matched_rule = fallback
            compiler_request = replace(
                compiler_request,
                cli_overrides=compiler_request.cli_overrides.model_copy(
                    update={"model": fallback_model}
                ),
                resolved_alias_entry=resolved_entry,
            )
            fallback_request = _compiler_request_for_fallback_candidate(
                compiler_request,
                matched_rule,
            )
            # Policy-derived fallbacks keep the matched rule's overrides at
            # CLI-equivalent precedence while stripping policy lists to avoid
            # recursive re-matching.
            compiler_result = _with_fallback_chain(
                compile_launch_params(fallback_request),
                effective_policies,
            )
            harness_id = HarnessId(compiler_result.harness)
            model_resolution_error = None
            harness_provenance = "availability-fallback"
            materialized = materialize_harness(
                compiler_result,
                harness_registry=surface.harness_registry,
            )
    assert materialized is not None

    model_token = compiler_result.model_token
    if model_token and resolved_entry is None and model_resolution_error is None:
        try:
            resolved_entry = surface.catalog.resolve_model(model_token)
        except ValueError:
            resolved_entry = None

    # If model resolution failed but harness is explicit, bind the raw
    # model string to the explicit harness instead of failing.
    explicit_request_harness = _is_pre_profile_explicit_layer(
        layer_index=harness_layer_index,
        pre_profile_layer_count=pre_profile_layer_count,
    )
    if model_resolution_error is not None and resolved_entry is None and model_token:
        if explicit_request_harness and not user_explicit_same_precedence:
            resolved_entry = AliasEntry(
                alias="",
                model_id=ModelId(model_token),
                resolved_harness=harness_id,
            )
            model_resolution_error = None
        else:
            raise model_resolution_error

    final_model, resolved_model_entry = _resolve_final_model(
        layer_model=model_token,
        resolved_entry=resolved_entry,
        harness_id=harness_id,
        config=surface.config,
        catalog=surface.catalog,
    )
    resolved_model_default_harness = _resolved_model_default_harness(resolved_model_entry)
    policy_warnings: list[CompositionWarning] = []

    if final_model and user_explicit_same_precedence:
        if model_resolution_error is not None:
            raise model_resolution_error
        validate_harness_compatibility(
            model=final_model,
            harness_id=harness_id,
            model_entry=resolved_model_entry,
            harness_registry=surface.harness_registry,
        )
    if (
        final_model
        and not user_explicit_same_precedence
        and resolved_model_entry is not None
        and resolved_model_default_harness is not None
        and harness_id != resolved_model_default_harness
        and compiler_result.field_provenance.harness_source
        not in (ProvenanceLevel.CLI, ProvenanceLevel.ENV)
    ):
        harness_source = "resolved"
        if compiler_result.field_provenance.harness_source in (
            ProvenanceLevel.PROFILE_MODEL_POLICY,
            ProvenanceLevel.AGENT_OVERLAY_POLICY,
        ):
            harness_source = "model-policy"
        validate_harness_compatibility(
            model=final_model,
            harness_id=harness_id,
            model_entry=resolved_model_entry,
            harness_registry=surface.harness_registry,
            harness_source=harness_source,
        )

    selected_entry: AliasEntry | None = resolved_model_entry
    model_selection: ModelSelectionContext | None = None
    if final_model:
        canonical_model_id = (
            str(selected_entry.model_id) if selected_entry is not None else final_model
        )
        harness_model_for_spec: str | None = None
        selected_entry_default_harness = _resolved_model_default_harness(selected_entry)
        if (
            selected_entry is not None
            and selected_entry_default_harness is not None
            and harness_id != selected_entry_default_harness
        ):
            computed = select_harness_model_id(
                model_entry=selected_entry,
                harness_id=harness_id,
                canonical_model_id=canonical_model_id,
            )
            if computed != canonical_model_id:
                harness_model_for_spec = computed
            raw_harness_candidates = getattr(selected_entry, "harness_candidates", ()) or ()
            harness_candidates = tuple(str(candidate) for candidate in raw_harness_candidates)
            if (
                harness_model_for_spec is None
                and str(harness_id) in harness_candidates
            ):
                policy_warnings.append(
                    CompositionWarning(
                        code="missing_runnable_path",
                        message=(
                            f"Harness '{harness_id}' is a supported candidate for model "
                            f"'{final_model}' but no harness-specific model ID is available. "
                            "Using canonical model ID."
                        ),
                    )
                )
        selected_model_token = (
            (selected_entry.alias.strip() or str(selected_entry.model_id))
            if selected_entry is not None
            else final_model
        )
        model_selection = ModelSelectionContext(
            requested_token=requested_token_for_selection or final_model,
            selected_model_token=selected_model_token,
            canonical_model_id=canonical_model_id,
            mars_provided_harness=(
                selected_entry.mars_provided_harness if selected_entry is not None else None
            ),
            resolved_entry=selected_entry,
            harness_provenance=harness_provenance or "",
            harness_model_id=harness_model_for_spec,
        )

    profile_policy_rule_matched = compiler_result.matched_model_policy and (
        compiler_result.model_policy_source is ProvenanceLevel.PROFILE_MODEL_POLICY
    )
    profile_policy_defaults = profile_overrides.execution_policy_scope()
    if (
        profile is not None
        and profile.model_policies
        and compiler_result.model_policy_source is ProvenanceLevel.PROFILE_MODEL_POLICY
        and not profile_policy_rule_matched
        and selected_entry is not None
        and profile_policy_defaults.model_dump(exclude_none=True)
    ):
        _LOGGER.debug(
            "No model-policies rule matched for '%s'; using generic profile model-policy defaults.",
            selected_entry.model_id,
        )
    resolved_routing, resolved_execution_policy = _build_final_resolved_views(
        base_resolved=base_resolved,
        compiler_result=compiler_result,
        harness_id=harness_id,
        supported_execution_policy_fields=surface.supported_execution_policy_fields,
    )

    skill_selected_model_token = (
        model_selection.selected_model_token if model_selection is not None else final_model
    )
    skill_canonical_model_id = (
        model_selection.canonical_model_id if model_selection is not None else final_model
    )
    resolved_skill_names = dedupe_skill_names((*profile_skills, *surface.requested_skills))
    resolved_skills = resolve_skills_from_profile(
        profile_skills=resolved_skill_names,
        project_root=project_root,
        readonly=surface.skills_readonly,
        harness_id=str(harness_id),
        selected_model_token=skill_selected_model_token,
        canonical_model_id=skill_canonical_model_id,
    )

    model_warning = "\n".join(compiler_result.warnings).strip() or None
    policy_warnings = [
        *_policy_warnings(
            profile_warning=profile_warning,
            model_warning=model_warning,
        ),
        *policy_warnings,
    ]

    return ResolvedLaunchPolicy(
        profile=profile,
        model=final_model,
        harness=harness_id,
        adapter=materialized.adapter,
        resolved_skills=resolved_skills,
        routing=resolved_routing,
        execution_policy=resolved_execution_policy,
        terminal_surface_mode=_resolve_terminal_surface_mode(harness_id=harness_id),
        field_provenance=compiler_result.field_provenance,
        model_selection=model_selection,
        fallback_chain=compiler_result.fallback_chain,
        warnings=tuple(policy_warnings),
        alias_catalog=alias_catalog,
    )


def resolve_policies(
    *,
    catalog: CatalogSession,
    layers: tuple[RuntimeOverrides, ...],
    config_overrides: RuntimeOverrides,
    config: MeridianConfig,
    harness_registry: HarnessRegistry,
    configured_default_harness: str = "claude",
    skills_readonly: bool = True,
) -> ResolvedLaunchPolicy:
    """Compatibility wrapper over the public shared launch policy boundary."""

    return resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=catalog,
            layers=layers,
            config_overrides=config_overrides,
            config=config,
            harness_registry=harness_registry,
            configured_default_harness=configured_default_harness,
            skills_readonly=skills_readonly,
        )
    )


__all__ = [
    "ModelSelectionContext",
    "ResolvedLaunchPolicy",
    "ResolvedPolicies",
    "SurfacePolicyInput",
    "match_model_policy",
    "resolve_launch_policy",
    "resolve_policies",
    "resolve_policy_fields",
    "validate_harness_compatibility",
]
