"""Harness launch spec — re-exports ResolvedLaunchSpec and enforcement utilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.bundle import HarnessBundle, get_bundle_registry
from meridian.lib.launch.launch_types import ResolvedLaunchSpec as ResolvedLaunchSpec


def _enforce_spawn_params_accounting(
    registry: Mapping[HarnessId, HarnessBundle[Any]] | None = None,
) -> None:
    """Fail fast when any adapter does not account for every SpawnParams field."""

    reg = registry if registry is not None else get_bundle_registry()
    expected = set(SpawnParams.model_fields)
    per_adapter: dict[HarnessId, frozenset[str]] = {}
    for harness_id, bundle in reg.items():
        handled = frozenset(bundle.adapter.handled_fields)
        per_adapter[harness_id] = handled

    drift = {
        harness_id: (expected - handled, set(handled) - expected)
        for harness_id, handled in per_adapter.items()
        if expected - handled or set(handled) - expected
    }
    if drift:
        raise ImportError(
            "SpawnParams per-adapter accounting drift. "
            + "; ".join(
                f"{harness_id.value}: missing={sorted(missing)}, stale={sorted(stale)}"
                for harness_id, (missing, stale) in drift.items()
            )
        )


__all__ = [
    "ResolvedLaunchSpec",
    "_enforce_spawn_params_accounting",
]
