"""Materialization layer: CompilerResult → runtime objects."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.compiler import CompilerResult


@dataclass(frozen=True)
class MaterializedLaunch:
    """Runtime objects produced from a CompilerResult."""

    result: CompilerResult
    harness_id: HarnessId
    adapter: SubprocessHarness


def materialize_harness(
    result: CompilerResult,
    *,
    harness_registry: HarnessRegistry,
) -> MaterializedLaunch:
    """Turn CompilerResult harness string into runtime adapter."""

    harness_id = HarnessId(result.harness)
    try:
        adapter = harness_registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError) as exc:
        supported = ", ".join(str(h) for h in harness_registry.ids())
        raise ValueError(
            f"Unknown or unsupported harness '{harness_id}'. Available harnesses: {supported}"
        ) from exc
    return MaterializedLaunch(result=result, harness_id=harness_id, adapter=adapter)
