"""Harness launch spec — re-exports ResolvedLaunchSpec and enforcement utilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.bundle import HarnessBundle, get_bundle_registry
from meridian.lib.harness.ids import HarnessId
from meridian.lib.launch.launch_types import ResolvedLaunchSpec as ResolvedLaunchSpec


def _enforce_spawn_params_accounting(
    registry: Mapping[HarnessId, HarnessBundle[Any]] | None = None,
) -> None:
    """Fail fast when SpawnParams fields are not claimed by adapters."""
    # New harnesses must be visible here via handled_fields to keep
    # cross-adapter SpawnParams coverage explicit.

    reg = registry if registry is not None else get_bundle_registry()
    expected = set(SpawnParams.model_fields)
    union: set[str] = set()
    per_adapter: dict[HarnessId, frozenset[str]] = {}
    for harness_id, bundle in reg.items():
        handled = frozenset(bundle.adapter.handled_fields)
        per_adapter[harness_id] = handled
        union |= handled

    missing = expected - union
    stale = union - expected
    if missing or stale:
        raise ImportError(
            "SpawnParams cross-adapter accounting drift. "
            f"Missing (no adapter claims these): {sorted(missing)}. "
            f"Stale (claimed but not on SpawnParams): {sorted(stale)}. "
            f"Per-adapter handled_fields: "
            f"{{ {', '.join(f'{h.value}: {sorted(f)}' for h, f in per_adapter.items())} }}"
        )


__all__ = [
    "ResolvedLaunchSpec",
    "_enforce_spawn_params_accounting",
]
