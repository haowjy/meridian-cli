"""Default single-turn drain coordinator for plain streaming harnesses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, SingleTurnDrainPolicy

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.streaming.spawn_session import DrainOutcome


@dataclass
class SingleTurnDrainCoordinator:
    """Finalize directly from terminal harness events."""

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    def default_policy(self) -> DrainPolicy:
        return SingleTurnDrainPolicy()

    def set_policy(self, policy: DrainPolicy) -> None:
        return

    def raw_terminal_frames_are_authoritative(self) -> bool:
        return True

    def next_timeout(self) -> float | None:
        return None

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_aux_wake(self) -> None:
        return

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return DrainLoopDecision()

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        return DrainTerminalDecision(
            recorded_outcome=outcome if action.terminate else None,
            emit_turn_boundary=action.emit_turn_boundary,
        )

    async def handle_timeout(self) -> DrainLoopDecision:
        return DrainLoopDecision()

    async def after_event(self) -> DrainLoopDecision:
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        return None

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        return DrainExitDecision(recorded_outcome=recorded_outcome)

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        return


__all__ = ["SingleTurnDrainCoordinator"]
