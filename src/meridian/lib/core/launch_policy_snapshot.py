"""Durable launch-policy snapshot DTO persisted with spawn state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.tools import ToolsField


class LaunchPolicySnapshot(BaseModel):
    """Durable JSON-safe resolved launch contract persisted with a spawn row."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    model: str
    harness: str
    agent: str | None = None
    agent_path: str | None = None
    agent_description: str = ""
    agent_profile_body: str = ""
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    loaded_skills: tuple[SkillContent, ...] = ()
    extra_args: tuple[str, ...] = ()
    execution_policy: ResolvedExecutionPolicy = Field(default_factory=ResolvedExecutionPolicy)
    tools: ToolsField | None = None
    mcp_tools: tuple[str, ...] = ()
    terminal_surface_mode: str | None = None
    matched_policy_rule: str | None = None
    model_selection_requested_token: str | None = None
    model_selection_selected_token: str | None = None
    model_selection_canonical_id: str | None = None
    model_selection_harness_provenance: str | None = None
    model_selection_harness_model_id: str | None = None
    field_provenance: dict[str, str] = Field(default_factory=dict)
    fallback_chain: tuple[dict[str, object], ...] = ()
    bundle_inventory_prompt: str | None = None


__all__ = ["LaunchPolicySnapshot"]
