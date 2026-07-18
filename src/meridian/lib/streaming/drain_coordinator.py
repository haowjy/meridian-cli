"""Shared coordinator seam for streaming drain completion policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from meridian.lib.streaming.drain_policy import DrainAction, DrainPolicy, SingleTurnDrainPolicy
from meridian.lib.streaming.drain_teardown import (
    DEFAULT_DRAIN_SESSION_TEARDOWN,
    DrainSessionTeardown,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.streaming.completion_contracts import CompletionCleanupRequest
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
    post_publication_cleanup: CompletionCleanupRequest | None = None


class AuxWakeCoordinator(Protocol):
    """Waiter-facing seam for non-event wake sources."""

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_aux_wake(self) -> None: ...


class DrainFinalizer(Protocol):
    """Optional post-finalization side effects for a drain plan."""

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None: ...


@dataclass(frozen=True)
class DrainPlan:
    """Complete drain-loop configuration for one active spawn."""

    coordinator: DrainCoordinator | None = None
    policy: DrainPolicy | None = None
    raw_terminal_frames_authoritative: bool = True
    on_policy_selected: Callable[[DrainPolicy], None] | None = None
    aux_wake: AuxWakeCoordinator | None = None
    handle_aux_wake: Callable[[], Awaitable[DrainLoopDecision]] | None = None
    finalizer: DrainFinalizer | None = None
    teardown: DrainSessionTeardown = DEFAULT_DRAIN_SESSION_TEARDOWN

    def selected_policy(self) -> DrainPolicy:
        return self.policy or SingleTurnDrainPolicy()

    def with_policy(self, policy: DrainPolicy) -> DrainPlan:
        return DrainPlan(
            coordinator=self.coordinator,
            policy=policy,
            raw_terminal_frames_authoritative=self.raw_terminal_frames_authoritative,
            on_policy_selected=self.on_policy_selected,
            aux_wake=self.aux_wake,
            handle_aux_wake=self.handle_aux_wake,
            finalizer=self.finalizer,
            teardown=self.teardown,
        )


class DrainCoordinator(Protocol):
    """Completion policy interface used by ``SpawnDrainLoop``.

    Implementations hide harness-specific waiting details: Pi disk/quiescence
    wakes, resident descendant polling, close classification, and finalization
    side effects.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def next_timeout(self) -> float | None: ...

    async def observe_event(self, event: RawHarnessEvent, transition: str | None) -> bool: ...

    def note_event_persisted(self, event: RawHarnessEvent) -> DrainLoopDecision: ...

    async def handle_terminal_event(
        self,
        event: RawHarnessEvent,
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

    async def execute_post_publication_cleanup(
        self,
        request: CompletionCleanupRequest,
    ) -> None: ...
