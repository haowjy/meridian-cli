from __future__ import annotations

from pathlib import Path

from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.resolve import (
    AgentLaunchInput,
    dedupe_skill_names,
    load_agent_profile_with_fallback,
    parse_duration_seconds,
    resolve_agent_launch_input,
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_notification_timeout_seconds,
    resolve_pi_task_ping_interval_seconds,
    validate_harness_compatibility,
)


def _registry_with_harnesses(*harness_ids: HarnessId) -> HarnessRegistry:
    base_registry = get_default_harness_registry()
    registry = HarnessRegistry()
    for harness_id in harness_ids:
        registry.register(base_registry.get(harness_id))
    return registry


def test_resolve_agent_launch_input_tri_state() -> None:
    assert resolve_agent_launch_input(None) == AgentLaunchInput()
    assert resolve_agent_launch_input("") == AgentLaunchInput(agent_opt_out=True)
    assert resolve_agent_launch_input("  ") == AgentLaunchInput(agent_opt_out=True)
    assert resolve_agent_launch_input(" coder ") == AgentLaunchInput(agent="coder")


def test_load_agent_profile_with_fallback_honors_agent_opt_out(
    tmp_path: Path,
) -> None:
    from tests.support.fixtures import write_agent

    write_agent(
        tmp_path,
        name="product-lead",
        model="claude-opus-4-6",
        body="# Product Lead",
    )
    (tmp_path / "meridian.toml").write_text(
        '[primary]\nagent = "product-lead"\n',
        encoding="utf-8",
    )

    profile, warning = load_agent_profile_with_fallback(
        project_root=tmp_path,
        configured_default="product-lead",
        agent_opt_out=True,
    )

    assert profile is None
    assert warning is None


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


def test_validate_harness_compatibility_skips_model_route_check_without_model_entry() -> None:
    validate_harness_compatibility(
        model="fake-model",
        harness_id=HarnessId.OPENCODE,
        model_entry=None,
        harness_registry=_registry_with_harnesses(HarnessId.OPENCODE),
    )


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


def test_parse_duration_seconds() -> None:
    assert parse_duration_seconds("90m") == 5400.0
    assert parse_duration_seconds("1h") == 3600.0
    assert parse_duration_seconds("12500") == 12500.0
    assert parse_duration_seconds("") is None


def test_resolve_pi_task_ping_interval_prefers_explicit() -> None:
    assert (
        resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=120.0,
            config_snapshot={"pi_task_ping_interval_seconds": 60.0},
        )
        == 120.0
    )


def test_resolve_pi_task_ping_interval_from_config() -> None:
    assert (
        resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=None,
            config_snapshot={"timeouts": {"pi_task_ping_interval_seconds": 3300.0}},
        )
        == 3300.0
    )


def test_resolve_pi_child_wave_timeout_from_meridian_config_field() -> None:
    from meridian.lib.config.settings import MeridianConfig

    snapshot = MeridianConfig(pi_child_wave_timeout_seconds=99.0).model_dump(
        mode="json",
        exclude_none=True,
    )
    assert (
        resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot=snapshot,
        )
        == 99.0
    )


def test_resolve_pi_task_ping_interval_from_meridian_config_field() -> None:
    from meridian.lib.config.settings import MeridianConfig

    snapshot = MeridianConfig(pi_task_ping_interval_seconds=1800.0).model_dump(
        mode="json",
        exclude_none=True,
    )
    assert (
        resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=None,
            config_snapshot=snapshot,
        )
        == 1800.0
    )
