"""The environment registry's own data invariants."""

from __future__ import annotations

from meridian.env_registry import ENV_VARS, EnvTier


def test_registry_prefix_matches_stability_tier() -> None:
    mismatches = [
        entry.name
        for entry in ENV_VARS
        if entry.name.startswith("_MERIDIAN_") != (entry.tier is EnvTier.INTERNAL)
    ]
    assert mismatches == []
