"""Fake-clock characterization of Pi drain coordination and current authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome, normalize_event
from meridian.lib.streaming.drain_policy import DrainAction
from tests.support.pi import PiDrainScenario, pi_event, start_row

_SPAWN_ID = SpawnId("p1")
_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
_AGENT_END = pi_event("agent_settled")
_start_coordinator = PiDrainScenario.start


def _start_row(
    runtime_root: Path, spawn_id: str, *, parent_id: str | None, status: SpawnStatus = "running"
) -> None:
    start_row(runtime_root, spawn_id, HarnessId.CODEX, parent_id, status=status)


@pytest.mark.asyncio
async def test_pi_eof_during_stabilization_cannot_bypass_child_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await _start_coordinator(tmp_path, monkeypatch)
    coordinator = started.coordinator
    try:
        await coordinator.wait_for_aux_wake()
        await coordinator.handle_aux_wake()
        await coordinator.observe_event(normalize_event(_AGENT_END))
        terminal = await coordinator.handle_terminal_event(
            _AGENT_END,
            _SUCCESS,
            _TERMINATE,
        )
        _start_row(tmp_path, "p-after-candidate", parent_id=str(_SPAWN_ID))

        assert terminal.recorded_outcome is None
        assert coordinator.handle_close(intentional_stop=False) == _SUCCESS
        assert coordinator.should_defer_close() is True

        started.clock.advance(0.05)
        requested = await coordinator.handle_timeout()
        assert requested.recorded_outcome is None
        await coordinator.wait_for_aux_wake()
        refreshed = await coordinator.handle_aux_wake()
        started.clock.advance(0.001)
        rejected = await coordinator.handle_timeout()

        assert refreshed.recorded_outcome is None
        assert rejected.recorded_outcome is None
        assert coordinator.classify_outstanding_work().spawn_children is True
    finally:
        await coordinator.stop()
