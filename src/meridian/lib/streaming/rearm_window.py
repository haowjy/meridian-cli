"""Absolute completion windows renewed only by budgeted explicit rearm signals."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state import spawn_store
from meridian.lib.streaming.completion_contracts import ProfileDecision


class RearmWindow:
    """Share signal latching, persisted rearm budget, and absolute-window arithmetic."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        deadline_seconds: float,
        rearm_budget: int | None,
    ) -> None:
        self._runtime_root = runtime_root
        self._spawn_id = spawn_id
        self._deadline_seconds = deadline_seconds
        self._rearm_budget = rearm_budget
        spawn = spawn_store.get_spawn(runtime_root, spawn_id)
        self._rearm_count = spawn.resident_rearm_count if spawn is not None else 0

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

    def grant_rearm(self, signaled: bool) -> bool:
        if not signaled:
            return False
        if self._rearm_budget is not None and self._rearm_count >= self._rearm_budget:
            return False
        self._rearm_count += 1
        spawn_store.update_spawn(
            self._runtime_root,
            self._spawn_id,
            resident_rearm_count=self._rearm_count,
        )
        return True
