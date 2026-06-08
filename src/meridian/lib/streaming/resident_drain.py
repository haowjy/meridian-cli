"""Resident-until-done coordinator for managed Codex/OpenCode drains."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, SingleTurnDrainPolicy

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.harness.connections.resident_backend import ResidentBackendControl
    from meridian.lib.streaming.spawn_session import DrainOutcome


_TIMEOUT_FLOOR_SECONDS = 0.001


@dataclass
class ResidentDrainCoordinator:
    """Own Codex/OpenCode post-turn waiting for Meridian-tracked child spawns."""

    runtime_root: Path
    spawn_id: SpawnId
    receiver: HarnessConnection[Any]
    deadline_seconds: float
    poll_seconds: float
    awaiting_outcome: TerminalEventOutcome | None = None
    deadline_monotonic: float | None = None

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        deadline_seconds: float | None,
        poll_seconds: float | None,
    ) -> ResidentDrainCoordinator:
        return cls(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=receiver,
            deadline_seconds=(
                deadline_seconds if deadline_seconds and deadline_seconds > 0 else 1800.0
            ),
            poll_seconds=poll_seconds if poll_seconds and poll_seconds > 0 else 10.0,
        )

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        self._set_awaiting_done(False)
        self.awaiting_outcome = None
        self.deadline_monotonic = None

    def default_policy(self) -> DrainPolicy:
        return SingleTurnDrainPolicy()

    def set_policy(self, policy: DrainPolicy) -> None:
        return

    def raw_terminal_frames_are_authoritative(self) -> bool:
        """Resident drains resolve terminal state after descendant work drains."""

        return False

    def observe_activity_transition(self, transition: str | None) -> None:
        """Clear resident-idle liveness once a new turn becomes active."""

        if transition == "turn_active":
            self._set_awaiting_done(False)
            self.awaiting_outcome = None
            self.deadline_monotonic = None

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        self.observe_activity_transition(transition)
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        if not action.terminate:
            self._set_awaiting_done(False)
            self.awaiting_outcome = None
            self.deadline_monotonic = None
            return DrainTerminalDecision(emit_turn_boundary=action.emit_turn_boundary)
        if outcome.status != "succeeded":
            self._set_awaiting_done(False)
            return DrainTerminalDecision(recorded_outcome=outcome)
        if not _has_outstanding_descendant_work(self.runtime_root, self.spawn_id):
            self._set_awaiting_done(False)
            return DrainTerminalDecision(recorded_outcome=outcome)

        self.awaiting_outcome = outcome
        self.deadline_monotonic = time.monotonic() + self.deadline_seconds
        self._set_awaiting_done(True)
        return DrainTerminalDecision(emit_turn_boundary=True)

    def next_timeout(self) -> float | None:
        if self.awaiting_outcome is None:
            return None
        now_monotonic = time.monotonic()
        remaining = self._deadline_remaining(now_monotonic)
        timeout = self.poll_seconds
        if remaining is not None:
            timeout = min(timeout, remaining)
        return max(timeout, _TIMEOUT_FLOOR_SECONDS)

    def _handle_poll(self) -> tuple[DrainLoopDecision, bool]:
        if self.awaiting_outcome is None:
            return (DrainLoopDecision(), False)
        now_monotonic = time.monotonic()
        deadline_expired = self._deadline_expired(now_monotonic)
        has_outstanding_work = _has_outstanding_descendant_work(
            self.runtime_root,
            self.spawn_id,
        )
        if deadline_expired or not has_outstanding_work:
            outcome = self.awaiting_outcome
            self._set_awaiting_done(False)
            self.awaiting_outcome = None
            self.deadline_monotonic = None
            return (
                DrainLoopDecision(recorded_outcome=outcome),
                deadline_expired and has_outstanding_work,
            )
        return (DrainLoopDecision(), False)

    async def handle_timeout(self) -> DrainLoopDecision:
        decision, reap_descendants = self._handle_poll()
        if reap_descendants:
            import asyncio

            await asyncio.to_thread(self.terminate_outstanding_descendants)
        return decision

    async def after_event(self) -> DrainLoopDecision:
        return await self.handle_timeout()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        """Classify stream close while resident-waiting.

        A completed turn is success only when Meridian deliberately stopped the
        session. EOF from a dead or otherwise unexpectedly closed backend wins as
        a failure while descendants are still outstanding.
        """

        if self.awaiting_outcome is None:
            return None
        if intentional_stop:
            return self.awaiting_outcome
        if _resident_health_status(self.receiver) == LivenessDecision.BACKEND_DEAD:
            return TerminalEventOutcome(
                status="failed",
                exit_code=1,
                error="backend_dead_while_awaiting_done",
            )
        return TerminalEventOutcome(
            status="failed",
            exit_code=1,
            error="stream_closed_while_awaiting_done",
        )

    def terminate_outstanding_descendants(self) -> None:
        """Reap tracked descendant process scopes after resident deadline expiry."""

        _terminate_descendant_spawn_tree(self.runtime_root, self.spawn_id)

    def wants_aux_wake(self) -> bool:
        return False

    async def wait_for_aux_wake(self) -> None:
        return

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return DrainLoopDecision()

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

    def _deadline_remaining(self, now_monotonic: float) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - now_monotonic

    def _deadline_expired(self, now_monotonic: float) -> bool:
        remaining = self._deadline_remaining(now_monotonic)
        return remaining is not None and remaining <= 0

    def _set_awaiting_done(self, awaiting_done: bool) -> None:
        resident_backend = _resident_backend(self.receiver)
        if resident_backend is not None:
            resident_backend.set_awaiting_done(awaiting_done)


def _resident_health_status(receiver: HarnessConnection[Any]) -> LivenessDecision | str:
    resident_backend = _resident_backend(receiver)
    if resident_backend is None:
        return "unsupported"
    try:
        return resident_backend.health_status()
    except Exception:
        return LivenessDecision.BACKEND_DEAD


def _resident_backend(receiver: HarnessConnection[Any]) -> ResidentBackendControl | None:
    try:
        return receiver.resident_backend
    except AttributeError:
        return None


def _terminate_descendant_spawn_tree(runtime_root: Path, spawn_id: SpawnId) -> None:
    from meridian.lib.state.spawn_tree import terminate_descendant_tree

    terminate_descendant_tree(
        runtime_root,
        spawn_id,
        reason="resident_deadline",
        grace_seconds=5.0,
    )


def _has_outstanding_descendant_work(runtime_root: Path, spawn_id: SpawnId) -> bool:
    from meridian.lib.state import spawn_store
    from meridian.lib.state.spawn_tree import has_outstanding_descendant_work

    return has_outstanding_descendant_work(str(spawn_id), spawn_store.list_spawns(runtime_root))


__all__ = ["ResidentDrainCoordinator"]
