"""Shared spawn state models and closed persisted vocabularies."""

from __future__ import annotations

from typing import Any, Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from meridian.lib.core.domain import SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import (
    OptionalPersistedChatId,
    OptionalPersistedHarnessSessionId,
    normalize_optional_identity,
)
from meridian.plugin_api.types import SpawnKind

LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
PersistedSpawnStatus = SpawnStatus | Literal["unknown"]


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
    harness_cumulative: dict[str, JsonValue] | None = None
    """The harness's running total at this launch's end; the next launch is seeded with it."""
    error: str | None = None
    origin: SpawnOrigin
    #: The reconciler decided this terminal row still owns managed-primary
    #: fallback scopes that a release path must tear down.
    managed_scopes_pending: bool = False


class RunBoundaryOutcome(BaseModel):
    """Post-exit identity evidence; ``current_chat_id`` alone routes continuation.

    ``verified`` means a post-exit key was proven natively and published as the
    current chat. Trampoline successors stay diagnostic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["verified", "unresolved", "mismatch"]
    trampoline_successor_id: str | None = None


class LegacyRunBoundaryOutcome(RunBoundaryOutcome):
    """Read-only old schema: validate retired evidence before normalizing it away."""

    exit_chat_id: OptionalPersistedChatId = None

    @model_validator(mode="after")
    def _verified_has_exit_chat(self) -> Self:
        if (self.status == "verified") != (self.exit_chat_id is not None):
            raise ValueError("exit_chat_id must be set exactly when status is verified")
        return self


class LaunchRef(BaseModel):
    """Which launch of the spawn the flat row describes (``launches/<n>/``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int = Field(ge=1)


def _reported_sum(prior: float, current: float | None) -> float | None:
    """A usage figure across launches: ``None`` where no launch reported it."""
    return None if current is None and not prior else prior + (current or 0)


class PriorUsage(BaseModel):
    """Usage of the launches before the current one; surfaces show it plus the current launch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    duration_secs: float = 0.0
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_is_estimate: bool = False
    harness_cumulative: dict[str, JsonValue] | None = None
    """The last finished launch's ``TerminalFacts.harness_cumulative``: what the running launch's
    extractor starts from, since its own ``terminal`` is not there yet."""

    def plus(self, terminal: TerminalFacts | None) -> PriorUsage:
        """These totals with one more launch's outcome added (cost estimates OR together)."""
        if terminal is None:
            return self
        return PriorUsage(
            duration_secs=self.duration_secs + (terminal.duration_secs or 0.0),
            total_cost_usd=self.total_cost_usd + (terminal.total_cost_usd or 0.0),
            input_tokens=self.input_tokens + (terminal.input_tokens or 0),
            output_tokens=self.output_tokens + (terminal.output_tokens or 0),
            cache_read_input_tokens=(
                self.cache_read_input_tokens + (terminal.cache_read_input_tokens or 0)
            ),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + (terminal.cache_creation_input_tokens or 0)
            ),
            reasoning_tokens=self.reasoning_tokens + (terminal.reasoning_tokens or 0),
            cost_is_estimate=self.cost_is_estimate or terminal.cost_is_estimate,
            # A launch that reported no total (it crashed) leaves the earlier one as the seed.
            harness_cumulative=terminal.harness_cumulative or self.harness_cumulative,
        )

    def totals(self, terminal: TerminalFacts | None) -> UsageTotals:
        """What a surface shows: these totals plus the current launch, ``None`` where
        nothing has reported that figure."""

        def count(prior: int, current: int | None) -> int | None:
            total = _reported_sum(prior, current)
            return None if total is None else int(total)

        t = terminal
        return UsageTotals(
            duration_secs=_reported_sum(self.duration_secs, t.duration_secs if t else None),
            total_cost_usd=_reported_sum(self.total_cost_usd, t.total_cost_usd if t else None),
            input_tokens=count(self.input_tokens, t.input_tokens if t else None),
            output_tokens=count(self.output_tokens, t.output_tokens if t else None),
            cache_read_input_tokens=count(
                self.cache_read_input_tokens, t.cache_read_input_tokens if t else None
            ),
            cache_creation_input_tokens=count(
                self.cache_creation_input_tokens, t.cache_creation_input_tokens if t else None
            ),
            reasoning_tokens=count(self.reasoning_tokens, t.reasoning_tokens if t else None),
            cost_is_estimate=self.cost_is_estimate or bool(t and t.cost_is_estimate),
        )


_NO_PRIOR_USAGE = PriorUsage()


class UsageTotals(BaseModel):
    """A spawn's usage across every launch; a figure no launch reported is ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    duration_secs: float | None
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None
    cost_is_estimate: bool


class OwedLaunch(BaseModel):
    """A background launch a continue moved past, still owed to the parent that started it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int = Field(ge=1)
    parent_id: str


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
    current_chat_id: OptionalPersistedChatId = None
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
    # State v4 (spawn-level). ``launch`` absent is a v3 row: its launch 1 lives in the spawn
    # directory, and the other two fields are never written for it.
    launch: LaunchRef | None = None
    prior_usage: PriorUsage = PriorUsage()
    owed_launches: tuple[OwedLaunch, ...] = ()

    @property
    def launch_number(self) -> int:
        """The current launch; a v3 row (``launch`` absent) has only its first."""
        return self.launch.n if self.launch else 1

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_exit_pointer(cls, data: object) -> object:
        """0.9.4 kept a verified exit chat in ``run_boundary``; read it as current."""
        if not isinstance(data, dict):
            return data
        raw = cast("dict[str, Any]", data)
        boundary = raw.get("run_boundary")
        if not isinstance(boundary, dict) or "exit_chat_id" not in boundary:
            return raw
        legacy = LegacyRunBoundaryOutcome.model_validate(boundary)
        current_chat_id = normalize_optional_identity(raw.get("current_chat_id"))
        normalized = dict(raw)
        normalized["run_boundary"] = legacy.model_dump(exclude={"exit_chat_id"})
        if current_chat_id is None and legacy.exit_chat_id is not None:
            normalized["current_chat_id"] = legacy.exit_chat_id
        return normalized


class SpawnRecord(SpawnStateFields):
    """Prompt-bearing state projection assembled from persisted spawn state."""

    prompt: str | None = None

    def index_json(self) -> str:
        """The history index's ``record_json``: this row without its default-valued v4 fields.

        Most rows are v3 or never continued, so these fields would add bytes the index
        validates on every query. ``model_validate_json`` restores the defaults, so the
        round trip is exact.
        """
        exclude: dict[str, Any] = {}
        if self.launch is None:
            exclude["launch"] = True
        if self.prior_usage == _NO_PRIOR_USAGE:
            exclude["prior_usage"] = True
        if not self.owed_launches:
            exclude["owed_launches"] = True
        if self.terminal is not None and self.terminal.harness_cumulative is None:
            exclude["terminal"] = {"harness_cumulative"}
        return self.model_dump_json(exclude=exclude)

    @property
    def total_usage(self) -> UsageTotals:
        """Usage across every launch, the current one included once finished. Every surface
        reads totals, so a continue never makes a number a user sees restart."""
        return self.prior_usage.totals(self.terminal)

    @property
    def total_duration_secs(self) -> float | None:
        """``total_usage.duration_secs`` without building the token totals."""
        terminal = self.terminal
        return _reported_sum(
            self.prior_usage.duration_secs, terminal.duration_secs if terminal else None
        )

    @property
    def total_cost_usd(self) -> float | None:
        """``total_usage.total_cost_usd`` without building the token totals."""
        terminal = self.terminal
        return _reported_sum(
            self.prior_usage.total_cost_usd, terminal.total_cost_usd if terminal else None
        )

    @property
    def has_no_session_of_its_own(self) -> bool:
        """A child that never started a session, so its ``chat_id`` is not evidence of one.

        This build reserves a fresh child with no chat (``owner_chat_id`` still names
        the caller). Earlier builds reserved it with the caller's chat as a
        placeholder. v0.3.2 through v0.4.5 rebound ``chat_id`` after the session
        started and v0.5.0 and later bind it atomically with ``session_instance_id``,
        so either way a child with no ``session_instance_id`` and a chat that is its
        owner's (or none, with an owner) never started. A crash between the start
        event and the rebind in v0.3.2 through v0.4.5 leaves the same shape and
        reads as unbound, like rows from before v0.3.2. Older chat-less rows
        (app spawns, pre-v3) have no owner and are not matched.
        """
        if self.kind != "child" or self.session_instance_id:
            return False
        if self.chat_id is None:
            return self.owner_chat_id is not None
        return self.chat_id == (self.owner_chat_id or self.chat_id)

    @property
    def entry_chat_id(self) -> str | None:
        """The chat this launch runs or will resume; None if it has no session yet."""
        return None if self.has_no_session_of_its_own else self.chat_id

    @property
    def continue_chat_id(self) -> str | None:
        """Last published current chat, otherwise the immutable entry chat."""
        if self.record_mode == "historical":
            return None
        if self.run_boundary is not None and self.run_boundary.status == "mismatch":
            return self.entry_chat_id
        return self.current_chat_id or self.entry_chat_id


__all__ = [
    "APP_LAUNCH_MODE",
    "BACKGROUND_LAUNCH_MODE",
    "FOREGROUND_LAUNCH_MODE",
    "_LAUNCH_MODE_VALUES",
    "CancelIntent",
    "LaunchMode",
    "LaunchPolicySnapshot",
    "LaunchRef",
    "OwedLaunch",
    "PersistedSpawnStatus",
    "PriorUsage",
    "RunBoundaryOutcome",
    "RunnerExitFacts",
    "SpawnKind",
    "SpawnOrigin",
    "SpawnRecord",
    "SpawnStateFields",
    "TerminalFacts",
    "TerminalSpawnStatus",
    "UsageTotals",
]
