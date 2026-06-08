"""Shared coordinator seam for streaming drain completion policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome


@dataclass(frozen=True)
class DrainTerminalDecision:
    """Coordinator decision after a terminal harness event."""

    recorded_outcome: TerminalEventOutcome | None = None
    emit_turn_boundary: bool = False


@dataclass(frozen=True)
class DrainLoopDecision:
    """Coordinator decision that may finish the drain loop."""

    recorded_outcome: TerminalEventOutcome | None = None


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

    def next_timeout(self) -> float | None: ...

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_aux_wake(self) -> None: ...

    async def handle_aux_wake(self) -> DrainLoopDecision: ...

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool: ...

    def note_event_persisted(self, event: HarnessEvent) -> None: ...

    def lifecycle_error_outcome(self) -> TerminalEventOutcome | None: ...

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision: ...

    async def handle_timeout(self) -> DrainLoopDecision: ...

    async def after_event(self) -> DrainLoopDecision: ...

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None: ...

    def failure_outcome_after_event(self) -> TerminalEventOutcome | None: ...

    def maybe_start_quiescence_after_event(self) -> None: ...

    def pending_children_at_exit(self) -> bool: ...

    async def cleanup_pending_children_at_exit(self) -> None: ...

    def fallback_error_without_recorded_outcome(self) -> str | None: ...

    def finalization_session_id(self, connection_session_id: str | None) -> str | None: ...

    def emit_session_phase_if_needed(self, session_id: str | None) -> None: ...

    def emit_finalized(self, *, status: str, exit_code: int, error: str | None) -> None: ...


class AuxWakeCoordinator(Protocol):
    """Small waiter-facing seam for non-event wake sources."""

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_aux_wake(self) -> None: ...
