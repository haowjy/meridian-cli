"""Shared spawn domain models.

This module owns spawn domain types that are shared by legacy event parsing,
v2 state persistence, lifecycle services, and terminal-write policy. Keeping
these types in a neutral module avoids import cycles between the store,
repository, reducer, and lifecycle layers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import SpawnStatus

LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
_AUTHORITATIVE_ORIGIN_VALUES: tuple[SpawnOrigin, ...] = (
    "runner",
    "launcher",
    "launch_failure",
    "cancel",
)
AUTHORITATIVE_ORIGINS: frozenset[SpawnOrigin] = frozenset(_AUTHORITATIVE_ORIGIN_VALUES)


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
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    launch_mode: LaunchMode | None
    worker_pid: int | None
    runner_pid: int | None
    status: SpawnStatus | Literal["unknown"]
    prompt: str | None
    started_at: str | None
    exited_at: str | None
    process_exit_code: int | None
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


__all__ = [
    "APP_LAUNCH_MODE",
    "AUTHORITATIVE_ORIGINS",
    "BACKGROUND_LAUNCH_MODE",
    "FOREGROUND_LAUNCH_MODE",
    "_AUTHORITATIVE_ORIGIN_VALUES",
    "_LAUNCH_MODE_VALUES",
    "LaunchMode",
    "SpawnOrigin",
    "SpawnRecord",
]
