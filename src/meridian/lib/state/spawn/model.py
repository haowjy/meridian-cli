"""Shared spawn domain models.

This module owns spawn domain types that are shared by legacy event parsing,
v2 state persistence, lifecycle services, and terminal-write policy. Keeping
these types in a neutral module avoids import cycles between the store,
repository, reducer, and lifecycle layers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.domain import SkillContent, SpawnStatus
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.launch.launch_types import TerminalSurfaceMode
from meridian.lib.tools import ToolsField

LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
TerminalSpawnStatus = Literal["succeeded", "failed", "cancelled"]
_AUTHORITATIVE_ORIGIN_VALUES: tuple[SpawnOrigin, ...] = (
    "runner",
    "launcher",
    "launch_failure",
    "cancel",
)
AUTHORITATIVE_ORIGINS: frozenset[SpawnOrigin] = frozenset(_AUTHORITATIVE_ORIGIN_VALUES)


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
    terminal_surface_mode: TerminalSurfaceMode | None = None
    matched_policy_rule: str | None = None
    model_selection_requested_token: str | None = None
    model_selection_selected_token: str | None = None
    model_selection_canonical_id: str | None = None
    model_selection_harness_provenance: str | None = None
    model_selection_harness_model_id: str | None = None
    field_provenance: dict[str, str] = Field(default_factory=dict)
    fallback_chain: tuple[dict[str, object], ...] = ()


class SpawnRecord(BaseModel):
    """Derived spawn state assembled from persisted spawn state."""

    model_config = ConfigDict(frozen=True)

    id: str
    chat_id: str | None
    parent_id: str | None
    model: str | None
    agent: str | None
    agent_path: str | None
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]
    harness: str | None
    kind: str
    desc: str | None
    work_id: str | None
    goal: str | None = None
    harness_session_id: str | None
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    launch_mode: LaunchMode | None
    worker_pid: int | None
    runner_pid: int | None
    runner_created_at_epoch: float | None
    status: SpawnStatus | Literal["unknown"]
    prompt: str | None
    started_at: str | None
    last_attempt_exited_at: str | None
    last_attempt_exit_code: int | None
    runner_exit_code: int | None
    runner_exit_status: TerminalSpawnStatus | None
    runner_exit_error: str | None
    runner_exit_at: str | None
    finished_at: str | None
    exit_code: int | None
    duration_secs: float | None
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None
    cost_is_estimate: bool
    error: str | None
    terminal_origin: SpawnOrigin | None
    process_scopes: tuple[dict[str, object], ...] | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None


__all__ = [
    "APP_LAUNCH_MODE",
    "AUTHORITATIVE_ORIGINS",
    "BACKGROUND_LAUNCH_MODE",
    "FOREGROUND_LAUNCH_MODE",
    "_AUTHORITATIVE_ORIGIN_VALUES",
    "_LAUNCH_MODE_VALUES",
    "LaunchMode",
    "LaunchPolicySnapshot",
    "SpawnOrigin",
    "SpawnRecord",
    "TerminalSpawnStatus",
]
