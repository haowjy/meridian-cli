"""Run-input aggregation stage for launch composition."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import ModelId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.launch.reference import ReferenceItem


class ResolvedRunInputs(BaseModel):
    """Factory-owned run inputs used to derive spec/argv/env stages."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    model: ModelId | None = None
    effort: str | None = None
    skills: tuple[str, ...] = ()
    agent: str | None = None
    adhoc_agent_payload: str = ""
    extra_args: tuple[str, ...] = ()
    project_root: str | None = None
    mcp_tools: tuple[str, ...] = ()
    projected_roots: tuple[Path, ...] = ()
    interactive: bool = False
    continue_harness_session_id: str | None = None
    continue_fork: bool = False
    appended_system_prompt: str | None = None
    report_output_path: str | None = None
    context_from_payload: tuple[str, ...] = ()
    reference_items: tuple[ReferenceItem, ...] = ()
    user_turn_content: str | None = None


def coerce_resolved_run_inputs(run_inputs: ResolvedRunInputs | SpawnParams) -> ResolvedRunInputs:
    """Normalize either DTO flavor into `ResolvedRunInputs`."""

    if isinstance(run_inputs, ResolvedRunInputs):
        return run_inputs
    return ResolvedRunInputs(**run_inputs.model_dump())


def to_spawn_params(run_inputs: ResolvedRunInputs | SpawnParams) -> SpawnParams:
    """Project stage-owned run inputs back to adapter launch params."""

    if isinstance(run_inputs, SpawnParams):
        return run_inputs
    payload = run_inputs.model_dump(exclude={"context_from_payload", "reference_items"})
    return SpawnParams(**payload)


__all__ = [
    "ResolvedRunInputs",
    "coerce_resolved_run_inputs",
    "to_spawn_params",
]
