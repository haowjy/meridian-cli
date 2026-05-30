"""Policy-resolution stage ownership for launch composition."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from meridian.lib.catalog.agent import AgentProfile
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
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.tools import ToolsField

from . import bundle_adapter
from .compiler import (
    FieldProvenance,
    match_model_policy,
)
from .composition import AvailableSkillEntry
from .launch_types import (
    CompositionWarning,
    ResolvedExecutionPolicy,
    ResolvedLaunchRouting,
    TerminalSurfaceMode,
)
from .policy_snapshot import (
    ReplayedModelSelection,
    replay_launch_policy_snapshot,
)
from .request import LaunchCompositionSurface, LaunchPolicySnapshot
from .resolve import (
    ResolvedSkills,
    dedupe_skill_names,
    load_agent_profile_with_fallback,
    resolve_skills_from_profile,
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
    skills_readonly: bool = True
    requested_skills: tuple[str, ...] = ()
    policy_snapshot: LaunchPolicySnapshot | None = None
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
    resolved_tools: ToolsField | None = None
    resolved_mcp_tools: tuple[str, ...] = ()
    terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED
    field_provenance: FieldProvenance = field(default_factory=FieldProvenance)
    matched_policy_rule: str | None = None
    model_selection: ModelSelectionContext | None = None
    fallback_chain: tuple[dict[str, object], ...] = ()
    warnings: tuple[CompositionWarning, ...] = ()
    alias_catalog: dict[str, AliasEntry] | None = None
    bundle_inventory_prompt: str | None = None
    bundle_available_skills: tuple[AvailableSkillEntry, ...] = ()
    """Available skill entries from bundle (on-demand, not loaded at launch)."""


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


def _resolve_bundle_model_default_harness(
    *,
    alias_catalog: dict[str, AliasEntry],
    selected_model_token: str,
    canonical_model_id: str,
) -> HarnessId | None:
    """Resolve bundle model default harness from loaded alias catalog only."""

    normalized_token = selected_model_token.strip()
    normalized_model_id = canonical_model_id.strip()

    for candidate in (normalized_token, normalized_model_id):
        if not candidate:
            continue
        entry = alias_catalog.get(candidate)
        if entry is None:
            continue
        try:
            return entry.harness
        except ValueError:
            continue

    if not normalized_model_id:
        return None
    for entry in alias_catalog.values():
        if str(entry.model_id) != normalized_model_id:
            continue
        try:
            return entry.harness
        except ValueError:
            continue
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
        raise TypeError("resolve_policy_fields() accepts a tier tuple only as its sole argument.")
    return tier


_EXECUTION_POLICY_PROVENANCE_KEYS: dict[ExecutionPolicyField, str] = {
    "effort": "effort_source",
    "sandbox": "sandbox_source",
    "approval": "approval_source",
    "autocompact": "autocompact_source",
    "autocompact_pct": "autocompact_pct_source",
    "timeout": "timeout_source",
}

# Routing precedence for the bundle path. Only explicit user overrides are
# forwarded into the bundle request.
_BUNDLE_ROUTING_PRECEDENCE: tuple[str, ...] = (
    "cli",
    "env",
)
_BUNDLE_ROUTING_SOURCES: frozenset[str] = frozenset({"cli", "env"})


def _first_routing_candidate(
    candidates: tuple[tuple[str, str | None], ...],
) -> tuple[str, str] | None:
    for source, value in candidates:
        if value is not None:
            return source, value
    return None


def _routing_rank(source: str) -> int:
    try:
        return _BUNDLE_ROUTING_PRECEDENCE.index(source)
    except ValueError:
        return len(_BUNDLE_ROUTING_PRECEDENCE)


def _resolve_bundle_routing(
    *,
    surface: SurfacePolicyInput,
) -> tuple[str | None, str | None, str | None, dict[str, str]]:
    """Resolve routing overrides + local routing provenance for the bundle path.

    Only explicit user overrides are forwarded to the bundle request.
    """

    model_candidate = _first_routing_candidate(
        (
            ("cli", surface.cli_overrides.model),
            ("env", surface.env_overrides.model),
        )
    )
    harness_candidate = _first_routing_candidate(
        (
            ("cli", surface.cli_overrides.harness),
            ("env", surface.env_overrides.harness),
        )
    )

    if (
        model_candidate is not None
        and harness_candidate is not None
        and _routing_rank(model_candidate[0]) < _routing_rank(harness_candidate[0])
    ):
        harness_candidate = None

    provenance_overrides: dict[str, str] = {}
    if model_candidate is not None and model_candidate[0] in _BUNDLE_ROUTING_SOURCES:
        provenance_overrides["model_source"] = model_candidate[0]
    if harness_candidate is not None and harness_candidate[0] in _BUNDLE_ROUTING_SOURCES:
        provenance_overrides["harness_source"] = harness_candidate[0]

    return (
        model_candidate[1]
        if model_candidate is not None and model_candidate[0] in _BUNDLE_ROUTING_SOURCES
        else None,
        harness_candidate[1]
        if harness_candidate is not None and harness_candidate[0] in _BUNDLE_ROUTING_SOURCES
        else None,
        model_candidate[1] if model_candidate is not None else None,
        provenance_overrides,
    )


def _resolve_bundle_execution_policy(
    *,
    surface: SurfacePolicyInput,
    bundle_execution_policy: RuntimeOverrides,
    profile_overrides: RuntimeOverrides,
) -> tuple[RuntimeOverrides, dict[str, str]]:
    """Resolve execution policy for the unified bundle path.

    Config defaults (config.primary.* for PRIMARY; empty for SPAWN_PREPARE)
    enter as the lowest-precedence layer so they fill gaps left by higher
    sources without overriding them.
    """

    config_policy = surface.config_overrides.execution_policy_scope(
        surface.supported_execution_policy_fields
    )
    policy_layers: tuple[tuple[str, RuntimeOverrides], ...] = (
        (
            "cli",
            surface.cli_overrides.execution_policy_scope(surface.supported_execution_policy_fields),
        ),
        (
            "env",
            surface.env_overrides.execution_policy_scope(surface.supported_execution_policy_fields),
        ),
        ("bundle", bundle_execution_policy),
        (
            "profile-default",
            profile_overrides.execution_policy_scope(surface.supported_execution_policy_fields),
        ),
        ("config-default", config_policy),
    )
    resolved = resolve_policy_fields(tuple(layer for _, layer in policy_layers))

    provenance_overrides: dict[str, str] = {}
    for field_name in surface.supported_execution_policy_fields:
        for source, layer in policy_layers:
            if getattr(layer, field_name) is None:
                continue
            if source != "bundle":
                provenance_overrides[_EXECUTION_POLICY_PROVENANCE_KEYS[field_name]] = source
            break
    return resolved, provenance_overrides


def _resolve_policy_from_bundle(surface: SurfacePolicyInput) -> ResolvedLaunchPolicy:
    """Resolve launch policy for PRIMARY and SPAWN_PREPARE via mars launch-bundle.

    Both surfaces share this single code path. The only differences are:
    - PRIMARY carries config.primary.* execution defaults in config_overrides;
      SPAWN_PREPARE passes empty config defaults.
    - Validation of primary-launch harness compatibility happens downstream in
      the composition layer (_prepare_primary_surface), not here.
    """

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
    profile_skills = dedupe_skill_names(profile.skills) if profile is not None else ()

    (
        bundle_model_override,
        bundle_harness_override,
        requested_model_token,
        routing_provenance_overrides,
    ) = _resolve_bundle_routing(
        surface=surface,
    )

    alias_catalog: dict[str, AliasEntry] = {}
    if requested_model_token:
        alias_catalog = surface.catalog.alias_map()

    resolved_skill_names = dedupe_skill_names((*profile_skills, *surface.requested_skills))
    bundle_request = bundle_adapter.BundleRequest(
        agent=profile.name if profile is not None else requested_agent,
        project_root=project_root,
        model_override=bundle_model_override,
        harness_override=bundle_harness_override,
        effort_override=explicit_user_overrides.effort,
        approval_override=explicit_user_overrides.approval,
        sandbox_override=explicit_user_overrides.sandbox,
        extra_skills=resolved_skill_names,
    )
    bundle_result = bundle_adapter.request_and_resolve(
        bundle_request,
        harness_registry=surface.harness_registry,
    )

    resolved_model = bundle_result.model
    resolved_harness = bundle_result.harness
    selected_model_token = bundle_result.model_token or resolved_model
    requested_token = requested_model_token or selected_model_token or resolved_model
    harness_provenance = routing_provenance_overrides.get(
        "harness_source",
        bundle_result.provenance.get("harness_source", ""),
    )

    model_selection = ModelSelectionContext(
        requested_token=requested_token or resolved_model,
        selected_model_token=selected_model_token or resolved_model,
        canonical_model_id=resolved_model,
        harness_provenance=harness_provenance,
        harness_model_id=bundle_result.harness_model,
    )

    bundle_execution_policy = bundle_result.execution_policy.as_overrides(
        supported_fields=surface.supported_execution_policy_fields
    )
    (
        resolved_execution_policy_overrides,
        execution_policy_provenance_overrides,
    ) = _resolve_bundle_execution_policy(
        surface=surface,
        bundle_execution_policy=bundle_execution_policy,
        profile_overrides=profile_overrides,
    )
    resolved_execution_policy = ResolvedExecutionPolicy(
        effort=resolved_execution_policy_overrides.effort,
        sandbox=resolved_execution_policy_overrides.sandbox,
        approval=resolved_execution_policy_overrides.approval,
        autocompact=resolved_execution_policy_overrides.autocompact,
        autocompact_pct=resolved_execution_policy_overrides.autocompact_pct,
        timeout=resolved_execution_policy_overrides.timeout,
    )

    resolved_skills = resolve_skills_from_profile(
        profile_skills=resolved_skill_names,
        project_root=project_root,
        readonly=surface.skills_readonly,
        harness_id=resolved_harness.value,
        selected_model_token=model_selection.selected_model_token,
        canonical_model_id=model_selection.canonical_model_id,
    )

    bundle_model_warning = "\n".join(bundle_result.warnings).strip() or None
    policy_warnings = list(
        _policy_warnings(
            profile_warning=profile_warning,
            model_warning=bundle_model_warning,
        )
    )

    # Post-bundle missing-runnable-path check:
    # - primary condition: no harness-specific model ID and the resolved harness
    #   differs from this model's catalog default harness.
    # - fallback condition: if catalog default harness is unknown, preserve the
    #   existing explicit-harness override warning behavior.
    default_harness = _resolve_bundle_model_default_harness(
        alias_catalog=alias_catalog,
        selected_model_token=selected_model_token,
        canonical_model_id=resolved_model,
    )
    if bundle_result.harness_model is None and resolved_model:
        if default_harness is not None and resolved_harness != default_harness:
            policy_warnings.append(
                CompositionWarning(
                    code="missing_runnable_path",
                    message=(
                        f"Harness '{resolved_harness}' is not the default harness "
                        f"'{default_harness}' for model '{resolved_model}', but no "
                        "harness-specific model ID is available. Using canonical "
                        "model ID."
                    ),
                )
            )
        elif default_harness is None and bundle_harness_override is not None:
            policy_warnings.append(
                CompositionWarning(
                    code="missing_runnable_path",
                    message=(
                        f"Harness '{resolved_harness}' was explicitly requested but no "
                        f"harness-specific model ID is available for '{resolved_model}'. "
                        "Using canonical model ID."
                    ),
                )
            )

    provenance_overrides = dict(routing_provenance_overrides)
    provenance_overrides.update(execution_policy_provenance_overrides)

    return bundle_adapter.bundle_to_resolved_policy(
        bundle=bundle_result,
        profile=profile,
        resolved_skills=resolved_skills,
        model_selection=model_selection,
        resolved_model=resolved_model,
        resolved_harness=resolved_harness,
        routing_model=model_selection.selected_model_token,
        routing_agent=base_resolved.agent,
        execution_policy=resolved_execution_policy,
        warnings=tuple(policy_warnings),
        alias_catalog=alias_catalog,
        harness_registry=surface.harness_registry,
        terminal_surface_mode=_resolve_terminal_surface_mode(harness_id=resolved_harness),
        provenance_overrides=provenance_overrides or None,
    )


def _resolve_policy_from_snapshot(
    *,
    surface: SurfacePolicyInput,
    snapshot: LaunchPolicySnapshot,
) -> ResolvedLaunchPolicy:
    """Resolve launch policy from a persisted launch-policy snapshot."""

    replayed = replay_launch_policy_snapshot(
        snapshot=snapshot,
        project_root=surface.catalog.project_root,
        harness_registry=surface.harness_registry,
        skills_readonly=surface.skills_readonly,
        alias_catalog=surface.catalog.alias_map(),
        resolve_terminal_surface_mode=_resolve_terminal_surface_mode,
        logger=_LOGGER,
    )
    return ResolvedLaunchPolicy(
        profile=replayed.profile,
        model=replayed.model,
        harness=replayed.harness,
        adapter=replayed.adapter,
        resolved_skills=replayed.resolved_skills,
        routing=replayed.routing,
        execution_policy=replayed.execution_policy,
        resolved_tools=replayed.resolved_tools,
        resolved_mcp_tools=replayed.resolved_mcp_tools,
        terminal_surface_mode=replayed.terminal_surface_mode,
        matched_policy_rule=replayed.matched_policy_rule,
        model_selection=_model_selection_from_replayed_snapshot(replayed.model_selection),
        fallback_chain=replayed.fallback_chain,
        warnings=(),
        alias_catalog=replayed.alias_catalog,
    )


def _model_selection_from_replayed_snapshot(
    replayed: ReplayedModelSelection | None,
) -> ModelSelectionContext | None:
    if replayed is None:
        return None
    return ModelSelectionContext(
        requested_token=replayed.requested_token,
        selected_model_token=replayed.selected_model_token,
        canonical_model_id=replayed.canonical_model_id,
        harness_provenance=replayed.harness_provenance,
        harness_model_id=replayed.harness_model_id,
    )


def resolve_launch_policy(surface: SurfacePolicyInput) -> ResolvedLaunchPolicy:
    """Resolve the shared launch policy boundary for one launch-like surface.

    PRIMARY and SPAWN_PREPARE both use the mars launch-bundle path.  DIRECT is
    handled separately (no policy resolution needed — already-resolved inputs).
    """

    if surface.policy_snapshot is not None:
        return _resolve_policy_from_snapshot(surface=surface, snapshot=surface.policy_snapshot)

    if surface.surface in {
        LaunchCompositionSurface.PRIMARY,
        LaunchCompositionSurface.SPAWN_PREPARE,
    }:
        return _resolve_policy_from_bundle(surface)

    raise ValueError(
        f"resolve_launch_policy called with unsupported surface '{surface.surface}'. "
        "Use compile_prepared_policy_surface for PRIMARY/SPAWN_PREPARE, "
        "or _build_direct_surface for DIRECT launches."
    )


def resolve_policies(
    *,
    catalog: CatalogSession,
    layers: tuple[RuntimeOverrides, ...],
    config_overrides: RuntimeOverrides,
    config: MeridianConfig,
    harness_registry: HarnessRegistry,
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
