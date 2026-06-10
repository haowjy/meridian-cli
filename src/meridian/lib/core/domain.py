"""Core frozen domain models."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.types import (
    ModelId,
    SpawnId,
)
from meridian.lib.core.util import FormatContext

SpawnStatus = Literal[
    "queued", "running", "finalizing", "succeeded", "failed", "cancelled", "timed_out"
]


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
