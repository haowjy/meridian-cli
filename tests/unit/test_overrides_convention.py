"""Structural convention tests: RuntimeOverrides field coverage across all config layers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def _env_overrides(overrides: dict[str, str]) -> Iterator[None]:
    """Temporarily set multiple environment variables."""
    old_values: dict[str, str | None] = {}
    for name, value in overrides.items():
        old_values[name] = os.environ.get(name)
        os.environ[name] = value
    try:
        yield
    finally:
        for name in overrides:
            if old_values[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_values[name]  # type: ignore[assignment]


_TEST_VALUES: dict[str, str] = {
    "model": "test-model",
    "harness": "claude",
    "agent": "coder",
    "effort": "high",
    "sandbox": "full-access",
    "approval": "auto",
    "autocompact": "200000",
    "autocompact_pct": "50",
    "timeout": "30.0",
}


def test_runtime_overrides_fields_are_exposed_across_env_config_and_cli_layers() -> None:
    from meridian.lib.config.settings import MeridianConfig, PrimaryConfig
    from meridian.lib.core.overrides import RuntimeOverrides
    from meridian.lib.launch.types import LaunchRequest
    from meridian.lib.ops.spawn.models import SpawnCreateInput

    runtime_fields = set(RuntimeOverrides.model_fields.keys())
    config_fields = set(PrimaryConfig.model_fields.keys())
    launch_fields = set(LaunchRequest.model_fields.keys())
    spawn_fields = set(SpawnCreateInput.model_fields.keys())

    for field_name in runtime_fields:
        assert field_name in config_fields
        assert field_name in launch_fields
        assert field_name in spawn_fields

    env_values = {f"MERIDIAN_{k.upper()}": v for k, v in _TEST_VALUES.items()}
    with _env_overrides(env_values):
        from_env = RuntimeOverrides.from_env()

    # harness is intentionally not read from env (spawn-local, not a policy override)
    env_excluded = {"harness"}
    for field_name in runtime_fields:
        if field_name in env_excluded:
            continue
        assert getattr(from_env, field_name) is not None

    primary = PrimaryConfig(
        model="test-model",
        harness="claude",
        agent="coder",
        effort="high",
        sandbox="full-access",
        approval="auto",
        autocompact=200000,
        autocompact_pct=50,
        timeout=30.0,
    )
    from_config = RuntimeOverrides.from_config(MeridianConfig(primary=primary))
    for field_name in runtime_fields:
        assert getattr(from_config, field_name) is not None


def test_resolve_precedence() -> None:
    from meridian.lib.core.overrides import RuntimeOverrides, resolve

    cli = RuntimeOverrides(model="cli-model")
    env = RuntimeOverrides(model="env-model", effort="high")
    profile = RuntimeOverrides(model="profile-model", effort="low", sandbox="full-access")
    config = RuntimeOverrides(
        model="config-model", effort="medium", sandbox="read-only", timeout=10.0
    )

    result = resolve(cli, env, profile, config)
    assert result.model == "cli-model"
    assert result.effort == "high"
    assert result.sandbox == "full-access"
    assert result.timeout == 10.0


def test_runtime_overrides_split_routing_from_execution_policy_fields() -> None:
    from meridian.lib.core.overrides import RuntimeOverrides

    overrides = RuntimeOverrides(
        model="gptmini",
        harness="codex",
        agent="reviewer",
        effort="high",
        sandbox="workspace-write",
        approval="auto",
        autocompact=200000,
        autocompact_pct=40,
        timeout=15.0,
    )

    assert overrides.routing_scope() == RuntimeOverrides(
        model="gptmini",
        harness="codex",
        agent="reviewer",
    )
    assert overrides.execution_policy_scope() == RuntimeOverrides(
        effort="high",
        sandbox="workspace-write",
        approval="auto",
        autocompact=200000,
        autocompact_pct=40,
        timeout=15.0,
    )
    assert overrides.execution_policy_scope(frozenset({"effort", "approval"})) == RuntimeOverrides(
        effort="high",
        approval="auto",
    )
    assert overrides.model_policy_scope() == overrides.execution_policy_scope()


def test_execution_policy_scope_rejects_unknown_supported_field_names() -> None:
    from meridian.lib.core.overrides import RuntimeOverrides

    overrides = RuntimeOverrides(effort="high", approval="auto")

    with pytest.raises(ValueError, match="Unknown execution policy field"):
        overrides.execution_policy_scope(frozenset({"effort", "typo"}))


def test_spawn_config_layer_does_not_set_harness() -> None:
    from meridian.lib.config.settings import MeridianConfig
    from meridian.lib.core.overrides import RuntimeOverrides

    overrides = RuntimeOverrides.from_spawn_config(
        MeridianConfig(
            default_model="gpt-5.4",
            default_harness="codex",
        )
    )

    assert overrides.model == "gpt-5.4"
    assert overrides.agent is None
    assert overrides.harness is None


# --- RuntimeOverrides.from_alias_entry ---


def test_from_alias_entry_returns_empty_overrides_without_alias_defaults() -> None:
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.core.overrides import RuntimeOverrides
    from meridian.lib.core.types import ModelId

    assert RuntimeOverrides.from_alias_entry(None) == RuntimeOverrides()
    assert RuntimeOverrides.from_alias_entry(
        AliasEntry(alias="sonnet", model_id=ModelId("claude-sonnet-4"))
    ) == RuntimeOverrides()


def test_from_alias_entry_exposes_alias_defaults_as_runtime_overrides() -> None:
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.core.overrides import RuntimeOverrides
    from meridian.lib.core.types import ModelId

    overrides = RuntimeOverrides.from_alias_entry(
        AliasEntry(
            alias="gpt",
            model_id=ModelId("gpt-5.5"),
            default_effort="low",
            default_autocompact=20000,
        )
    )

    assert overrides == RuntimeOverrides(effort="low", autocompact=20000)


def test_from_alias_entry_treats_auto_effort_as_unset() -> None:
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.core.overrides import RuntimeOverrides
    from meridian.lib.core.types import ModelId

    overrides = RuntimeOverrides.from_alias_entry(
        AliasEntry(
            alias="gpt",
            model_id=ModelId("gpt-5.5"),
            default_effort="auto",
            default_autocompact=30000,
        )
    )

    assert overrides == RuntimeOverrides(autocompact=30000)
