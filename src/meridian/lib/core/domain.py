from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeVar, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from typing_extensions import TypeIs

from meridian.lib.core.types import (
    ModelId,
    SpawnId,
)
from meridian.lib.core.util import FormatContext


class SpawnLifecycleClass(StrEnum):
    """Lifecycle partition for persisted spawn statuses."""

    ACTIVE = "active"
    TERMINAL = "terminal"


class SpawnStatus(StrEnum):
    """Single authority for the persisted spawn status vocabulary."""

    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


_StatusT = TypeVar("_StatusT", bound=StrEnum)


def _derive_spawn_status_sets(
    classifications: dict[_StatusT, SpawnLifecycleClass],
) -> tuple[frozenset[_StatusT], frozenset[_StatusT], frozenset[_StatusT]]:

    all_statuses = frozenset(classifications)
    active = frozenset(
        status
        for status, lifecycle_class in classifications.items()
        if lifecycle_class is SpawnLifecycleClass.ACTIVE
    )
    terminal = frozenset(
        status
        for status, lifecycle_class in classifications.items()
        if lifecycle_class is SpawnLifecycleClass.TERMINAL
    )
    return all_statuses, active, terminal


_SPAWN_STATUS_CLASSIFICATIONS = {
    SpawnStatus.QUEUED: SpawnLifecycleClass.ACTIVE,
    SpawnStatus.RUNNING: SpawnLifecycleClass.ACTIVE,
    SpawnStatus.FINALIZING: SpawnLifecycleClass.ACTIVE,
    SpawnStatus.SUCCEEDED: SpawnLifecycleClass.TERMINAL,
    SpawnStatus.FAILED: SpawnLifecycleClass.TERMINAL,
    SpawnStatus.CANCELLED: SpawnLifecycleClass.TERMINAL,
    SpawnStatus.TIMED_OUT: SpawnLifecycleClass.TERMINAL,
}
ALL_SPAWN_STATUSES, ACTIVE_SPAWN_STATUSES, TERMINAL_SPAWN_STATUSES = _derive_spawn_status_sets(
    _SPAWN_STATUS_CLASSIFICATIONS
)


# Leave time for the terminal write before cancellation escalates to SIGKILL,
# which would lose the runner's usage.
CANCEL_GRACE_SECS = 5.0
SIGNAL_STOP_BUDGET_SECS = CANCEL_GRACE_SECS - 2.0

FAILURE_SPAWN_STATUSES: frozenset[str] = frozenset({"failed", "timed_out"})


def is_active_spawn_status(status: str) -> bool:
    return status in ACTIVE_SPAWN_STATUSES


def is_terminal_spawn_status(status: str) -> TypeIs[TerminalSpawnStatus]:
    return status in TERMINAL_SPAWN_STATUSES


def parse_terminal_status(status: str) -> TerminalSpawnStatus:
    if is_terminal_spawn_status(status):
        return status
    raise ValueError(f"Invalid terminal spawn status: {status!r}")


def coerce_spawn_status(status: str) -> SpawnStatus:

    if status in ALL_SPAWN_STATUSES:
        return cast("SpawnStatus", status)
    return SpawnStatus.FAILED


if set(_SPAWN_STATUS_CLASSIFICATIONS) != set(SpawnStatus):
    raise ImportError("Spawn status classifications must account for every SpawnStatus member")

type TerminalSpawnStatus = Literal["succeeded", "failed", "cancelled", "timed_out"]

if frozenset(get_args(TerminalSpawnStatus.__value__)) != frozenset(
    status.value for status in TERMINAL_SPAWN_STATUSES
):
    raise ImportError("TerminalSpawnStatus must account for every terminal SpawnStatus member")


class TokenUsage(BaseModel):
    """Token usage measured for a spawn."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_cost_usd: float | None = None
    cost_is_estimate: bool = False
    #: The harness's own running total at the end of this launch, for harnesses whose
    #: reports are cumulative over a resumed session. Opaque outside that harness's
    #: extractor, which reads it back as the seed of the next launch.
    harness_cumulative: dict[str, JsonValue] | None = None


class Spawn(BaseModel):
    """Spawn aggregate root."""

    model_config = ConfigDict(frozen=True)

    spawn_id: SpawnId
    prompt: str
    model: ModelId
    status: SpawnStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


SkillDeliveryMode = Literal["inline", "named"]
"""How a launch delivers its loaded skills: full text, or a line naming them."""


class PrimarySessionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    harness: str
    model: str
    agent: str
    agent_path: str
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]


class SkillContent(BaseModel):
    """Loaded skill body."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    content: str
    path: str
    skill_type: str = "reference"
    detail: str = ""

    def format_text(self, ctx: FormatContext | None = None) -> str:
        return f"{self.name}: {self.description}\n\n{self.content}"


class AvailableSkillEntry(BaseModel):
    """Lightweight entry for an on-demand skill (name and type, no body)."""

    model_config = ConfigDict(frozen=True)

    name: str
    skill_type: str = "reference"
    source_path: str | None = None
