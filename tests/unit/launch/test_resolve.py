from __future__ import annotations

from meridian.lib.catalog.model_aliases import AliasEntry, RunnablePath
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.prompt import dedupe_skill_names as prompt_dedupe_skill_names
from meridian.lib.launch.resolve import (
    dedupe_skill_names,
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_notification_timeout_seconds,
    select_harness_model_id,
    validate_harness_compatibility,
)


def _registry_with_harnesses(*harness_ids: HarnessId) -> HarnessRegistry:
    base_registry = get_default_harness_registry()
    registry = HarnessRegistry()
    for harness_id in harness_ids:
        registry.register(base_registry.get(harness_id))
    return registry


def test_dedupe_skill_names_is_importable_from_resolve() -> None:
    assert callable(dedupe_skill_names)


def test_dedupe_skill_names_is_reexported_from_prompt() -> None:
    assert prompt_dedupe_skill_names is dedupe_skill_names


def test_dedupe_skill_names_preserves_first_seen_order() -> None:
    assert dedupe_skill_names([" alpha ", "beta", "alpha", "", " beta ", "gamma "]) == (
        "alpha",
        "beta",
        "gamma",
    )


def test_validate_harness_compatibility_accepts_harness_candidate_route() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        harness_candidates=("codex", "opencode"),
    )

    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.OPENCODE,
        model_entry=model_entry,
        harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
    )


def test_validate_harness_compatibility_allows_harness_outside_candidates() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        harness_candidates=("codex", "opencode"),
    )

    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.CLAUDE,
        model_entry=model_entry,
        harness_registry=_registry_with_harnesses(
            HarnessId.CLAUDE,
            HarnessId.CODEX,
            HarnessId.OPENCODE,
        ),
    )


def test_validate_harness_compatibility_allows_harness_with_empty_candidates() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        harness_candidates=(),
    )

    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.OPENCODE,
        model_entry=model_entry,
        harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
    )


def test_validate_harness_compatibility_allows_policy_reroute_outside_candidates() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        harness_candidates=("codex",),
    )

    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.OPENCODE,
        model_entry=model_entry,
        harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
    )


def test_validate_harness_compatibility_skips_model_route_check_without_model_entry() -> None:
    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.OPENCODE,
        model_entry=None,
        harness_registry=_registry_with_harnesses(HarnessId.OPENCODE),
    )


def test_select_harness_model_id_returns_harness_specific_id_for_matching_path() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        runnable_paths=(
            RunnablePath(
                harness="opencode",
                harness_model_id="provider/opencode-model",
            ),
        ),
    )

    selected = select_harness_model_id(
        model_entry=model_entry,
        harness_id=HarnessId.OPENCODE,
        canonical_model_id="fake-model",
    )

    assert selected == "provider/opencode-model"


def test_select_harness_model_id_returns_canonical_when_path_does_not_match() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
        runnable_paths=(
            RunnablePath(
                harness="codex",
                harness_model_id="provider/codex-model",
            ),
        ),
    )

    selected = select_harness_model_id(
        model_entry=model_entry,
        harness_id=HarnessId.OPENCODE,
        canonical_model_id="fake-model",
    )

    assert selected == "fake-model"


def test_select_harness_model_id_returns_canonical_without_model_entry() -> None:
    selected = select_harness_model_id(
        model_entry=None,
        harness_id=HarnessId.OPENCODE,
        canonical_model_id="fake-model",
    )

    assert selected == "fake-model"


def test_select_harness_model_id_returns_canonical_with_empty_paths() -> None:
    model_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.CODEX,
    )

    selected = select_harness_model_id(
        model_entry=model_entry,
        harness_id=HarnessId.OPENCODE,
        canonical_model_id="fake-model",
    )

    assert selected == "fake-model"


def test_resolve_pi_notification_timeout_prefers_explicit_timeout() -> None:
    assert (
        resolve_pi_notification_timeout_seconds(
            explicit_timeout_seconds=42.0,
            config_snapshot={"wait_timeout_minutes": 30.0},
        )
        == 42.0
    )


def test_resolve_pi_notification_timeout_uses_config_wait_timeout_default() -> None:
    assert (
        resolve_pi_notification_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot={"wait_timeout_minutes": 30.0},
        )
        == 1800.0
    )


def test_resolve_pi_child_wave_timeout_defaults_to_five_minutes() -> None:
    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot=None,
        )
        == 300.0
    )


def test_resolve_pi_child_wave_timeout_prefers_explicit_timeout() -> None:
    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=42.0,
            config_snapshot={"pi_child_wave_timeout_seconds": 10.0},
        )
        == 42.0
    )


def test_resolve_pi_child_wave_timeout_uses_config_override() -> None:
    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot={"timeouts": {"pi_child_wave_timeout_seconds": 12.5}},
        )
        == 12.5
    )
