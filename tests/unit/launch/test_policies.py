from __future__ import annotations

from pathlib import Path

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.config.models import MeridianConfig, PrimaryConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.compiler import ModelPolicyRule
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.launch.policies import (
    SurfacePolicyInput,
    _resolve_bundle_execution_policy,
    _resolve_bundle_routing,
    match_model_policy,
    resolve_policy_fields,
)
from meridian.lib.launch.request import LaunchCompositionSurface


def _surface(*layers: RuntimeOverrides) -> SurfacePolicyInput:
    config = MeridianConfig(primary=PrimaryConfig(autocompact=70000))
    return SurfacePolicyInput(
        surface=LaunchCompositionSurface.PRIMARY,
        catalog=CatalogSession(Path("/tmp/project")),
        layers=layers,
        config_overrides=RuntimeOverrides.from_config(config),
        config=config,
        harness_registry=get_default_harness_registry(),
    )


def _terminal_surface_mode(*, harness_id: HarnessId) -> TerminalSurfaceMode:
    _ = harness_id
    return TerminalSurfaceMode.PTY_MEDIATED


def test_resolve_policy_fields_resolves_each_field_independently() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(timeout=12.5),
        RuntimeOverrides(sandbox="workspace-write"),
        RuntimeOverrides(effort="medium", sandbox="read-only"),
        RuntimeOverrides(approval="confirm", autocompact=70000),
        RuntimeOverrides(effort="low", approval="auto", autocompact=30000),
    )

    assert resolved.timeout == 12.5
    assert resolved.sandbox == "workspace-write"
    assert resolved.effort == "medium"
    assert resolved.approval == "confirm"
    assert resolved.autocompact == 70000


def test_model_policy_scope_strips_routing_fields() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(
            model="gpt55",
            harness="codex",
            agent="reviewer",
            effort="high",
        ).model_policy_scope(),
        RuntimeOverrides(approval="auto").model_policy_scope(),
    )

    assert resolved.model is None
    assert resolved.harness is None
    assert resolved.agent is None
    assert resolved.effort == "high"
    assert resolved.approval == "auto"


def test_match_model_policy_first_match_wins_by_list_order() -> None:
    winner = match_model_policy(
        model_policies=(
            ModelPolicyRule(
                match_type="model-glob",
                match_value="gpt-*",
                overrides={"effort": "low"},
            ),
            ModelPolicyRule(
                match_type="alias",
                match_value="fast",
                overrides={"effort": "medium"},
            ),
            ModelPolicyRule(
                match_type="model",
                match_value="gpt-5.5",
                overrides={"effort": "high"},
            ),
        ),
        canonical_model_id="gpt-5.5",
        selected_model_token="fast",
    )

    assert winner is not None
    assert (winner.match_type, winner.match_value) == ("model-glob", "gpt-*")


def test_bundle_execution_policy_resolves_precedence_and_provenance_per_field() -> None:
    surface = _surface(
        RuntimeOverrides(timeout=12.0),
        RuntimeOverrides(sandbox="workspace-write"),
    )

    resolved, provenance = _resolve_bundle_execution_policy(
        surface=surface,
        bundle_execution_policy=RuntimeOverrides(effort="high", timeout=45.0),
        profile_overrides=RuntimeOverrides(approval="confirm"),
    )

    assert resolved.timeout == 12.0
    assert resolved.sandbox == "workspace-write"
    assert resolved.effort == "high"
    assert resolved.approval == "confirm"
    assert resolved.autocompact == 70000
    assert provenance == {
        "timeout_source": "cli",
        "sandbox_source": "env",
        "approval_source": "profile-default",
        "autocompact_source": "config-default",
    }


def test_routing_precedence_drops_lower_priority_harness_override() -> None:
    model, harness, requested_model, provenance = _resolve_bundle_routing(
        surface=_surface(
            RuntimeOverrides(model="gpt55"),
            RuntimeOverrides(model="env-model", harness="claude"),
        )
    )

    assert model == "gpt55"
    assert harness is None
    assert requested_model == "gpt55"
    assert provenance == {"model_source": "cli"}
