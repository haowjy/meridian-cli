"""Harness-agnostic contracts for idle sensors."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent


@dataclass(frozen=True)
class IdleEvent:
    """One harness observation relevant to an idle stretch."""

    kind: Literal["turn_end", "user_prompt", "busy", "idle"]
    harness_session_id: str
    turn_id: str | None
    timestamp: float


@dataclass(frozen=True)
class IdleFacts:
    """Live facts used to decide whether compaction is safe."""

    draft: Literal["yes", "no", "unknown"]
    busy: bool
    agents_running: int
    context_tokens: int | None
    harness_autocompact_off: bool


@dataclass(frozen=True)
class CompactResult:
    """Result of asking the harness to compact its current session."""

    result: Literal["ok", "failed", "vetoed"]
    reason: str | None = None


@dataclass(frozen=True)
class IdleSensorContext:
    """Managed-primary resources available for one sensor's lifetime."""

    connection: HarnessConnection[Any]
    harness_id: HarnessId
    harness_session_id: str
    env: Mapping[str, str]
    tmux_pane: str | None
    tui_alive: Callable[[], bool]
    spawn_dir: Path


class IdleSensor(Protocol):
    """Harness-provided idle observations and actions."""

    def on_raw_event(self, event: RawHarnessEvent) -> None: ...

    def events(self) -> AsyncIterator[IdleEvent]: ...

    async def facts(self) -> IdleFacts: ...

    async def compact(self) -> CompactResult: ...


__all__ = [
    "CompactResult",
    "IdleEvent",
    "IdleFacts",
    "IdleSensor",
    "IdleSensorContext",
]
