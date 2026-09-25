"""Shared spawn state models and closed persisted vocabularies."""

from __future__ import annotations

from typing import Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES, SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import OptionalPersistedChatId, OptionalPersistedHarnessSessionId

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

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_at: str
    exit_code: int
    error: str | None
    requested_by: Literal["user", "system"] = "user"


class RunnerExitFacts(BaseModel):
    """Complete runner-resolved terminal intent, persisted before finalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: TerminalSpawnStatus
    exit_code: int
    error: str | None
    exited_at: str


class TerminalFacts(BaseModel):
    """Complete persisted facts for a finalized spawn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    #: The reconciler decided this terminal row still owns managed-primary
    #: fallback scopes that a release path must tear down.
    managed_scopes_pending: bool = False


class RunBoundaryOutcome(BaseModel):
    """Identity verification at the boundary between an entry and exit chat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["verified", "unresolved", "mismatch"]
    exit_chat_id: OptionalPersistedChatId = None
    trampoline_successor_id: str | None = None

    @model_validator(mode="after")
    def _verified_has_exit_chat(self) -> Self:
        if (self.status == "verified") != (self.exit_chat_id is not None):
            raise ValueError("exit_chat_id must be set exactly when status is verified")
        return self


class SpawnStateFields(BaseModel):
    """Fields shared by the stored and prompt-bearing state projections."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    history_id: UUID | None = None
    record_mode: Literal["live", "historical"] = "live"
    session_instance_id: str | None = None
    parent_history_id: UUID | None = None
    owner_history_id: UUID | None = None
    forked_from_history_id: UUID | None = None
    retained_history_ids: tuple[UUID, ...] = ()
    state_revision: int = Field(default=0, ge=0)
    chat_id: OptionalPersistedChatId = None
    run_boundary: RunBoundaryOutcome | None = None
    owner_chat_id: OptionalPersistedChatId = None
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
    harness_session_id: OptionalPersistedHarnessSessionId = None
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
    def _translate_dogfood_boundary(cls, value: object) -> object:
        """Read PR-1 dogfood rows while keeping the current schema canonical."""
        if not isinstance(value, dict):
            return value
        data = dict(cast("dict[str, object]", value))
        entry_chat_id = data.pop("entry_chat_id", None)
        exit_chat_id = data.pop("exit_chat_id", None)
        exit_identity = data.pop("exit_identity", None)
        trampoline = data.pop("trampoline_successor_id", None)
        if data.get("chat_id") is None and entry_chat_id is not None:
            data["chat_id"] = entry_chat_id
        if data.get("run_boundary") is None and exit_identity is not None:
            data["run_boundary"] = {
                "status": exit_identity,
                "exit_chat_id": exit_chat_id,
            }
        if trampoline is not None:
            boundary = RunBoundaryOutcome.model_validate(
                data.get("run_boundary") or {"status": "unresolved"}
            ).model_dump()
            data["run_boundary"] = {**boundary, "trampoline_successor_id": trampoline}
        return data

class SpawnRecord(SpawnStateFields):
    """Prompt-bearing state projection assembled from persisted spawn state."""

    prompt: str | None = None

    @property
    def continue_chat_id(self) -> str | None:
        """Verified terminal exit, otherwise the immutable entry chat."""
        if self.status in TERMINAL_SPAWN_STATUSES and (
            self.run_boundary is not None and self.run_boundary.status == "verified"
        ):
            return self.run_boundary.exit_chat_id
        return self.chat_id


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
    "RunBoundaryOutcome",
    "RunnerExitFacts",
    "SpawnKind",
    "SpawnOrigin",
    "SpawnRecord",
    "SpawnStateFields",
    "TerminalFacts",
    "TerminalSpawnStatus",
]
