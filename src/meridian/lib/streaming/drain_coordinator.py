"""Shared coordinator seam for streaming drain completion policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.streaming.spawn_session import DrainOutcome


@dataclass(frozen=True)
class DrainTerminalDecision:
    """Coordinator decision after a terminal harness event."""

    recorded_outcome: TerminalEventOutcome | None = None
    emit_turn_boundary: bool = False


@dataclass(frozen=True)
class DrainLoopDecision:
    """Coordinator decision that may finish the drain loop."""

    recorded_outcome: TerminalEventOutcome | None = None


@dataclass(frozen=True)
class DrainExitDecision:
    """Coordinator decision when the event stream exits before finalization."""

    recorded_outcome: TerminalEventOutcome | None = None
    fallback_error: str | None = None


class DrainCoordinator(Protocol):
    """Completion policy interface used by ``SpawnDrainLoop``.

    Implementations hide harness-specific waiting details: Pi disk/quiescence
    wakes, resident descendant polling, close classification, and finalization
    side effects.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def default_policy(self) -> DrainPolicy: ...

    def set_policy(self, policy: DrainPolicy) -> None: ...

    def raw_terminal_frames_are_authoritative(self) -> bool: ...

    def next_timeout(self) -> float | None: ...

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_aux_wake(self) -> None: ...

    async def handle_aux_wake(self) -> DrainLoopDecision: ...

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool: ...

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision: ...

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision: ...

    async def handle_timeout(self) -> DrainLoopDecision: ...

    async def after_event(self) -> DrainLoopDecision: ...

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None: ...

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision: ...

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None: ...


class AuxWakeCoordinator(Protocol):
    """Small waiter-facing seam for non-event wake sources."""

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_aux_wake(self) -> None: ...
