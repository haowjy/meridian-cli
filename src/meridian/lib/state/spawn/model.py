"""Shared spawn state models and closed persisted vocabularies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot

LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
TerminalSpawnStatus = Literal["succeeded", "failed", "cancelled", "timed_out"]
PersistedSpawnStatus = SpawnStatus | Literal["unknown"]
_AUTHORITATIVE_ORIGIN_VALUES: tuple[SpawnOrigin, ...] = (
    "runner",
    "launcher",
    "launch_failure",
    "cancel",
)
AUTHORITATIVE_ORIGINS: frozenset[SpawnOrigin] = frozenset(_AUTHORITATIVE_ORIGIN_VALUES)


class CancelIntent(BaseModel):
    """Durable spawn-level cancellation request."""

    model_config = ConfigDict(frozen=True)

    requested_at: str
    exit_code: int
    error: str | None
    requested_by: Literal["user", "system"] = "user"


class SpawnStateFields(BaseModel):
    """Fields shared by the stored and prompt-bearing state projections."""

    model_config = ConfigDict(frozen=True)

    id: str
    chat_id: str | None = None
    owner_chat_id: str | None = None
    parent_id: str | None = None
    originating_bash_id: str | None = None
    model: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    harness: str | None = None
    kind: str = "child"
    desc: str | None = None
    work_id: str | None = None
    goal: str | None = None
    display_label: str | None = None
    harness_session_id: str | None = None
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    launch_mode: LaunchMode | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    runner_created_at_epoch: float | None = None
    resident_rearm_count: int = 0
    status: PersistedSpawnStatus = "unknown"
    started_at: str | None = None
    last_attempt_exited_at: str | None = None
    last_attempt_exit_code: int | None = None
    runner_exit_code: int | None = None
    runner_exit_status: TerminalSpawnStatus | None = None
    runner_exit_error: str | None = None
    runner_exit_at: str | None = None
    cancel_intent: CancelIntent | None = None
    finished_at: str | None = None
    published_at: str | None = None
    exit_code: int | None = None
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False
    error: str | None = None
    terminal_origin: SpawnOrigin | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None


class SpawnRecord(SpawnStateFields):
    """Prompt-bearing state projection assembled from persisted spawn state."""

    prompt: str | None = None


__all__ = [
    "APP_LAUNCH_MODE",
    "AUTHORITATIVE_ORIGINS",
    "BACKGROUND_LAUNCH_MODE",
    "FOREGROUND_LAUNCH_MODE",
    "_AUTHORITATIVE_ORIGIN_VALUES",
    "_LAUNCH_MODE_VALUES",
    "CancelIntent",
    "LaunchMode",
    "LaunchPolicySnapshot",
    "PersistedSpawnStatus",
    "SpawnOrigin",
    "SpawnRecord",
    "SpawnStateFields",
    "TerminalSpawnStatus",
]
