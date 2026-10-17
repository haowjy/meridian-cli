"""Keep Claude child streams open through native background-task follow-ups."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.semantics import NormalizedHarnessEvent, TerminalEventOutcome
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.utils.json_shapes import json_list

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.streaming.completion_contracts import CompletionCleanupRequest
    from meridian.lib.streaming.drain_policy import DrainAction


class ClaudeDrainCoordinator:
    """Use Claude's current task snapshot, not tool calls or accumulated history."""

    def __init__(self, *, deadline_seconds: float | None) -> None:
        self._deadline_seconds = (
            deadline_seconds if deadline_seconds and deadline_seconds > 0 else 3300.0
        )
        self._pending = False
        self._deadline_at: float | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def next_timeout(self) -> float | None:
        if self._deadline_at is None:
            return None
        return max(0.0, self._deadline_at - time.monotonic())

    async def observe_event(self, event: NormalizedHarnessEvent) -> NormalizedHarnessEvent:
        raw = event.raw
        if raw.harness_id != "claude":
            return event
        if raw.event_type == "system" and raw.payload.get("subtype") == "background_tasks_changed":
            tasks = json_list(raw.payload.get("tasks"))
            if tasks is None:
                raise ValueError("Claude background_tasks_changed missing tasks array")
            self._pending = bool(tasks)
        terminal = event.semantics.terminal
        if terminal is not None and terminal.status == SpawnStatus.SUCCEEDED and self._pending:
            if self._deadline_at is None:
                self._deadline_at = time.monotonic() + self._deadline_seconds
            # Do not retain an early success: even an empty snapshot must be
            # followed by Claude's own follow-up result before we can finalize.
            return replace(event, semantics=replace(event.semantics, terminal=None))
        return event

    def note_event_delivered(self, event: RawHarnessEvent) -> DrainLoopDecision:
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: RawHarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        self._deadline_at = None
        return DrainTerminalDecision(
            recorded_outcome=outcome if action.terminate else None,
            emit_turn_boundary=action.emit_turn_boundary,
        )

    async def handle_timeout(self) -> DrainLoopDecision:
        return await self.after_event()

    async def after_event(self) -> DrainLoopDecision:
        if self._deadline_at is not None and time.monotonic() >= self._deadline_at:
            return DrainLoopDecision(
                recorded_outcome=TerminalEventOutcome(
                    status=SpawnStatus.TIMED_OUT,
                    exit_code=1,
                    error="resident_deadline_expired",
                )
            )
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        if self._pending or self._deadline_at is not None:
            return TerminalEventOutcome(
                status=SpawnStatus.FAILED,
                exit_code=1,
                error="stream_closed_while_awaiting_done",
            )
        return None

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        return DrainExitDecision(recorded_outcome=recorded_outcome)

    async def execute_post_publication_cleanup(self, request: CompletionCleanupRequest) -> None:
        pass
