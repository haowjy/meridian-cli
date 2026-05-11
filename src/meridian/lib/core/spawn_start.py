"""Creation-time spawn metadata shared across lifecycle/store start seams."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpawnStartMetadata:
    """User-facing metadata captured when a spawn row is first created."""

    desc: str | None = None
    work_id: str | None = None
    goal: str | None = None
