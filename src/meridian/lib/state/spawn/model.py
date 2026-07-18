"""Shared spawn state models and closed persisted vocabularies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from meridian.lib.core.domain import (
    TERMINAL_SPAWN_STATUSES,
    SpawnStatus,
    TerminalSpawnStatus,
)
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import ChatId, HarnessSessionId, normalize_optional_identity

LaunchMode = Literal["background", "foreground", "app"]
SpawnKind = Literal["child", "primary", "streaming"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
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


class RunnerExitFacts(BaseModel):
    """Complete runner-resolved terminal intent, persisted before finalization."""

    model_config = ConfigDict(frozen=True)

    status: TerminalSpawnStatus
    exit_code: int
    error: str | None
    exited_at: str


class TerminalFacts(BaseModel):
    """Complete persisted facts for a finalized spawn."""

    model_config = ConfigDict(frozen=True)

    status: TerminalSpawnStatus
    exit_code: int
    finished_at: str
    published_at: str
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False
    error: str | None = None
    origin: SpawnOrigin


class SpawnStateFields(BaseModel):
    """Fields shared by the stored and prompt-bearing state projections."""

    model_config = ConfigDict(frozen=True)

    id: str
    chat_id: ChatId | None = None
    owner_chat_id: ChatId | None = None
    parent_id: str | None = None
    originating_bash_id: str | None = None
    model: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    harness: str | None = None
    kind: SpawnKind = "child"
    desc: str | None = None
    work_id: str | None = None
    goal: str | None = None
    display_label: str | None = None
    harness_session_id: HarnessSessionId | None = None
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
    runner_exit: RunnerExitFacts | None = None
    cancel_intent: CancelIntent | None = None
    terminal: TerminalFacts | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None

    @model_validator(mode="before")
    @classmethod
    def require_discriminated_terminal_facts(cls, value: object) -> object:
        """Require terminal status and terminal facts to appear as one coherent state."""

        if not isinstance(value, dict):
            return value
        retired_fact_fields = {
            "runner_exit_code",
            "runner_exit_status",
            "runner_exit_error",
            "runner_exit_at",
            "finished_at",
            "published_at",
            "exit_code",
            "duration_secs",
            "total_cost_usd",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
            "cost_is_estimate",
            "error",
            "terminal_origin",
        }
        present_retired_fields = retired_fact_fields.intersection(value)
        if present_retired_fields:
            raise ValueError(
                f"flat lifecycle facts are not parseable: {sorted(present_retired_fields)}"
            )
        status = value.get("status", "unknown")
        if not isinstance(status, str):
            return value
        terminal = value.get("terminal")
        if status in TERMINAL_SPAWN_STATUSES:
            if terminal is None:
                raise ValueError("terminal spawn status requires complete terminal facts")
            if not isinstance(terminal, (dict, TerminalFacts)):
                return value
            terminal_status = (
                terminal.get("status") if isinstance(terminal, dict) else terminal.status
            )
            if not isinstance(terminal_status, str):
                return value
            if terminal_status != status:
                raise ValueError("terminal facts status must match spawn status")
        elif terminal is not None:
            raise ValueError("nonterminal spawn status cannot carry terminal facts")
        return value

    @field_validator("chat_id", "owner_chat_id", "harness_session_id", mode="before")
    @classmethod
    def normalize_persisted_identity(cls, value: object) -> str | None:
        return normalize_optional_identity(value)

    @property
    def runner_exit_status(self) -> TerminalSpawnStatus | None:
        return self.runner_exit.status if self.runner_exit is not None else None

    @property
    def runner_exit_code(self) -> int | None:
        return self.runner_exit.exit_code if self.runner_exit is not None else None

    @property
    def runner_exit_error(self) -> str | None:
        return self.runner_exit.error if self.runner_exit is not None else None

    @property
    def runner_exit_at(self) -> str | None:
        return self.runner_exit.exited_at if self.runner_exit is not None else None

    @property
    def finished_at(self) -> str | None:
        return self.terminal.finished_at if self.terminal is not None else None

    @property
    def published_at(self) -> str | None:
        return self.terminal.published_at if self.terminal is not None else None

    @property
    def exit_code(self) -> int | None:
        return self.terminal.exit_code if self.terminal is not None else None

    @property
    def duration_secs(self) -> float | None:
        return self.terminal.duration_secs if self.terminal is not None else None

    @property
    def total_cost_usd(self) -> float | None:
        return self.terminal.total_cost_usd if self.terminal is not None else None

    @property
    def input_tokens(self) -> int | None:
        return self.terminal.input_tokens if self.terminal is not None else None

    @property
    def output_tokens(self) -> int | None:
        return self.terminal.output_tokens if self.terminal is not None else None

    @property
    def cache_read_input_tokens(self) -> int | None:
        return self.terminal.cache_read_input_tokens if self.terminal is not None else None

    @property
    def cache_creation_input_tokens(self) -> int | None:
        return (
            self.terminal.cache_creation_input_tokens if self.terminal is not None else None
        )

    @property
    def reasoning_tokens(self) -> int | None:
        return self.terminal.reasoning_tokens if self.terminal is not None else None

    @property
    def cost_is_estimate(self) -> bool:
        return self.terminal.cost_is_estimate if self.terminal is not None else False

    @property
    def error(self) -> str | None:
        return self.terminal.error if self.terminal is not None else None

    @property
    def terminal_origin(self) -> SpawnOrigin | None:
        return self.terminal.origin if self.terminal is not None else None


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
    "RunnerExitFacts",
    "SpawnKind",
    "SpawnOrigin",
    "SpawnRecord",
    "SpawnStateFields",
    "TerminalFacts",
    "TerminalSpawnStatus",
]
