"""Durable launch-policy snapshot DTO persisted with spawn state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.domain import AvailableSkillEntry, SkillContent, SkillDeliveryMode
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.tools import ToolRule, ToolsField


class LaunchPolicySnapshot(BaseModel):
    """Durable JSON-safe resolved launch contract persisted with a spawn row."""

    model_config = ConfigDict(frozen=True)

    model: str
    harness: str
    agent: str | None = None
    agent_opt_out: bool = False
    agent_profile: dict[str, object] | None = None
    skills: tuple[str, ...] = ()
    loaded_skills: tuple[SkillContent, ...] = ()
    # On-demand skills the first launch listed under `# Available Skills`. Only names,
    # descriptions and paths: an exact continue recomposes the same listing without
    # re-resolving the bundle. Empty on snapshots an older build wrote.
    available_skills: tuple[AvailableSkillEntry, ...] = ()
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    execution_policy: ResolvedExecutionPolicy = Field(default_factory=ResolvedExecutionPolicy)
    tool_rules: tuple[ToolRule, ...] = ()
    # Candidate platform denies named by Mars for this harness. A snapshot written before
    # this field replays with the names a fresh Mars bundle gives for the same harness.
    platform_tool_rules: tuple[ToolRule, ...] = ()
    # Pre-rules shape; only snapshots an older build wrote still carry it. Never written.
    tools: ToolsField | None = None
    # Legacy read-only compatibility. Older snapshots stored native permission
    # names here; replay deliberately ignores them because they are not MCP config.
    mcp_tools: tuple[str, ...] = ()
    matched_policy_rule: str | None = None
    model_selection_requested_token: str | None = None
    model_selection_selected_token: str | None = None
    model_selection_canonical_id: str | None = None
    model_selection_provider_constraint: str | None = None
    model_selection_harness_provenance: str | None = None
    model_selection_harness_model_id: str | None = None
    field_provenance: dict[str, str] = Field(default_factory=dict)
    selection_report: dict[str, object] | None = None
    bundle_inventory_prompt: str | None = None
    # A snapshot an older build wrote has no mode; those launches delivered inline.
    skill_delivery: SkillDeliveryMode = "inline"


__all__ = ["LaunchPolicySnapshot"]
