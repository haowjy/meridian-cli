"""Absolute completion windows renewed only by budgeted explicit rearm signals."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import consume_resident_signals
from meridian.lib.streaming.completion_contracts import (
    CompletionDirectives,
    CompletionEvaluation,
    NudgeUrgency,
    ProfileDecision,
)
from meridian.lib.streaming.completion_nudge import COMPLETION_NUDGE_INTERVAL_SECONDS


class RearmWindow:
    """Share signal latching, persisted rearm budget, and absolute-window arithmetic."""

    def __init__(
        self, *, runtime_root: Path, spawn_id: SpawnId,
        deadline_seconds: float, rearm_budget: int | None,
    ) -> None:
        self._runtime_root = runtime_root
        self._spawn_id = spawn_id
        self._deadline_seconds = deadline_seconds
        self._rearm_budget = rearm_budget
        spawn = spawn_store.get_spawn(runtime_root, spawn_id)
        self._rearm_count = spawn.resident_rearm_count if spawn is not None else 0
        self._done_requested = False
        # Short configured windows must offer a rearm opportunity before expiry.
        self.nudge_interval = min(COMPLETION_NUDGE_INTERVAL_SECONDS, deadline_seconds / 2)

    def consume_directives(self) -> CompletionDirectives:
        signals = consume_resident_signals(self._runtime_root, self._spawn_id)
        self._done_requested = self._done_requested or signals.done
        return CompletionDirectives(
            done=self._done_requested,
            rearm=self._grant_rearm(signals.rearm),
        )

    def clear(self) -> None:
        self._done_requested = False

    def deadline_for(self, now: float) -> float:
        return now + self._deadline_seconds

    def timeout_decision(self) -> ProfileDecision:
        error = (
            "resident_rearm_budget_exhausted"
            if self._rearm_budget is not None and self._rearm_count >= self._rearm_budget
            else "resident_deadline_expired"
        )
        return ProfileDecision(
            action="cleanup",
            outcome=TerminalEventOutcome(status=SpawnStatus.TIMED_OUT, exit_code=1, error=error),
            cleanup_reason=error,
        )

    def _grant_rearm(self, signaled: bool) -> bool:
        if not signaled:
            return False
        if self._rearm_budget is not None and self._rearm_count >= self._rearm_budget:
            return False
        self._rearm_count += 1
        spawn_store.update_spawn(
            self._runtime_root, self._spawn_id, resident_rearm_count=self._rearm_count,
        )
        return True

    def nudge_urgency(self, context: CompletionEvaluation) -> NudgeUrgency:
        deadline_at = context.state.deadline_at
        remaining = None if deadline_at is None else deadline_at - context.now
        if remaining is not None and remaining < self.nudge_interval:
            return "timeout_soon"
        return "normal"
