"""Harness-agnostic contracts for idle sensors."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent

COMPACT_SCREEN_NOT_RECOGNIZED = "screen-not-recognized"


@dataclass(frozen=True)
class IdleEvent:
    """One harness observation relevant to an idle stretch."""

    kind: Literal["turn_end", "user_prompt"]
    harness_session_id: str
    turn_id: str | None
    implies_return: bool = False
    input_count: int | None = None
    ttl_seconds: int | None = None
    last_user_text: str | None = None
    last_assistant_text: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class PinnedIdleSession:
    """Minimum persisted state exposed to a harness-native event parser."""

    last_input_count: int | None


@dataclass(frozen=True)
class NativeIdleHook:
    """One intercepted native callback's observation and downstream delegation."""

    observe: Callable[
        [str, Callable[[str], PinnedIdleSession | None]], IdleEvent | None
    ]
    delegate: Callable[[str, Mapping[str, str]], None]


@dataclass(frozen=True)
class IdleFacts:
    """Live facts used to decide whether compaction is safe."""

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
    spawn_id: SpawnId | None = None


class IdleSensor(Protocol):
    """Harness-provided idle observations and actions."""

    def on_raw_event(self, event: RawHarnessEvent) -> None: ...

    def events(self) -> AsyncIterator[IdleEvent]: ...

    async def facts(self) -> IdleFacts: ...

    async def compact(self) -> CompactResult: ...


@dataclass(frozen=True)
class CacheTtl:
    """A cache lifetime and whether it is a heuristic rather than a known TTL."""

    seconds: int
    estimated: bool


class DetectIdleTtl(Protocol):
    """Resolve one harness session's prompt-cache lifetime and confidence."""

    def __call__(
        self,
        *,
        session_id: str,
        native_store: Path | None,
        provider: str | None,
        env: Mapping[str, str],
    ) -> CacheTtl | None: ...


__all__ = [
    "COMPACT_SCREEN_NOT_RECOGNIZED",
    "CacheTtl",
    "CompactResult",
    "DetectIdleTtl",
    "IdleEvent",
    "IdleFacts",
    "IdleSensor",
    "IdleSensorContext",
    "NativeIdleHook",
    "PinnedIdleSession",
]
