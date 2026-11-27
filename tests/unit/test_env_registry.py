"""The environment registry's own data invariants."""

from __future__ import annotations

from meridian.env_registry import ENV_VARS, EnvTier, session_free_env
from meridian.lib.config.loader import OPTION_CATALOG


def test_registry_prefix_matches_stability_tier() -> None:
    mismatches = [
        entry.name
        for entry in ENV_VARS
        if entry.name.startswith("_MERIDIAN_") != (entry.tier is EnvTier.INTERNAL)
    ]
    assert mismatches == []


def test_registry_names_every_environment_variable_the_config_catalog_reads() -> None:
    read = {name for option in OPTION_CATALOG.options for name in option.env_vars}

    assert sorted(read - {entry.name for entry in ENV_VARS}) == []


def test_session_free_env_drops_identity_and_hook_payload_but_keeps_config() -> None:
    env = session_free_env(
        {
            "MERIDIAN_SPAWN_ID": "p9",
            "_MERIDIAN_DEPTH": "2",
            "MERIDIAN_CONTEXT_KB_DIR": "/kb",
            "MERIDIAN_SPAWN_STATUS": "success",
            "MERIDIAN_HOME": "/x",
            "MERIDIAN_CONFIG": "/x/config.toml",
            "MERIDIAN_HOOKS_ENABLED": "false",
            "PATH": "/bin",
        }
    )

    assert env == {
        "MERIDIAN_HOME": "/x",
        "MERIDIAN_CONFIG": "/x/config.toml",
        "MERIDIAN_HOOKS_ENABLED": "false",
        "PATH": "/bin",
    }
