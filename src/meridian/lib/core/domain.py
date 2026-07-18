"""Core frozen domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field

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
    """Derive all/active/terminal sets solely from member classifications."""

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


class Spawn(BaseModel):
    """Spawn aggregate root."""

    model_config = ConfigDict(frozen=True)

    spawn_id: SpawnId
    prompt: str
    model: ModelId
    status: SpawnStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IndexReport(BaseModel):
    """Skill index operation summary."""

    model_config = ConfigDict(frozen=True)

    indexed_count: int

    def format_text(self, ctx: FormatContext | None = None) -> str:
        return f"skills.reindex  ok  indexed={self.indexed_count}"


class SkillManifest(BaseModel):
    """Skill manifest metadata."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    path: str = ""


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
