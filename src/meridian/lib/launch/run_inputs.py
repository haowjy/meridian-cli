"""Run-input aggregation stage for launch composition — legacy re-exports."""

from __future__ import annotations

from meridian.lib.harness.adapter import SpawnParams

# Backwards compatibility re-export — the canonical type is SpawnParams.
ResolvedRunInputs = SpawnParams


def coerce_resolved_run_inputs(run_inputs: SpawnParams) -> SpawnParams:
    """Identity — kept for call-site compatibility during transition."""
    return run_inputs


def to_spawn_params(run_inputs: SpawnParams) -> SpawnParams:
    """Identity — kept for call-site compatibility during transition."""
    return run_inputs


__all__ = [
    "ResolvedRunInputs",
    "coerce_resolved_run_inputs",
    "to_spawn_params",
]
