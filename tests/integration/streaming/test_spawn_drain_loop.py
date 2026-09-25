"""Spawn drain persistence-ordering regression tests."""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    EvidenceActivity,
    EvidenceEventDecision,
    NudgeUrgency,
    ProfileDecision,
    ProfileExitDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.drain_coordinator import (
    DrainCoordinator,
    DrainExitDecision,
    DrainLoopDecision,
    DrainPlan,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_wait import (
    DrainClosedWake,
    DrainInputWaiter,
    DrainTimeoutWake,
    _cancel_task,
)
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop
from meridian.lib.streaming.spawn_manager import SpawnManager
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession
from tests.support.fakes import FakeClock
from tests.support.pi import PiDrainScenario

_SPAWN_ID = SpawnId("p-persist-order")
Call = tuple[str, RawHarnessEvent]


def test_manager_emit_runs_hooks_then_fan_out(tmp_path: Path) -> None:
    manager = SpawnManager(tmp_path / "runtime", tmp_path)
    event = RawHarnessEvent(event_type="message", harness_id="fake", payload={})
    calls: list[str] = []

    def failing_hook(_event: RawHarnessEvent) -> None:
        calls.append("failing-hook")
        raise RuntimeError("hook failure")

    manager.register_event_hook(_SPAWN_ID, failing_hook)
    manager.register_event_hook(_SPAWN_ID, lambda _event: calls.append("hook"))
    manager._fan_out_event = lambda _spawn_id, _event: calls.append("fan-out")  # type: ignore[method-assign]

    manager.emit_event(_SPAWN_ID, event)
    assert calls == ["failing-hook", "hook", "fan-out"]


def test_history_blind_mode_traps_reads_in_cli_subprocesses(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    if request.config.getoption("--runner-history") != "off":
        pytest.skip("requires --runner-history=off")

    history_path = tmp_path / "spawns" / "p-child" / "history.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text("{}\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).read_text()",
            str(history_path),
        ],
        capture_output=True,
        check=False,
        env=os.environ.copy(),
        text=True,
    )

    assert result.returncode != 0
    assert "runner history read is disabled" in result.stderr


@pytest.mark.asyncio
async def test_cancel_task_retrieves_closed_stream_exception() -> None:
    loop = asyncio.get_running_loop()
    leaked: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: leaked.append(context))
    try:
        pending_event = loop.create_future()
        pending_event.set_exception(StopAsyncIteration())

        await _cancel_task(pending_event)
        del pending_event
        gc.collect()
        await asyncio.sleep(0)

        assert leaked == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancel_task_logs_unexpected_completed_task_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pending_event = asyncio.get_running_loop().create_future()
    pending_event.set_exception(RuntimeError("event source broke during close"))

    with caplog.at_level("WARNING"):
        await _cancel_task(pending_event)

    assert "event source broke during close" in caplog.text


@pytest.mark.asyncio
async def test_closed_event_input_still_arbitrates_completion_timeout() -> None:
    async def closed_events() -> AsyncIterator[RawHarnessEvent]:
        if False:
            yield RawHarnessEvent(event_type="unused", harness_id="test", payload={})

    class _NoAux:
        def wants_aux_wake(self) -> bool:
            return False

        async def wait_for_aux_wake(self) -> None:
            raise AssertionError("auxiliary wake was not requested")

    waiter = DrainInputWaiter(closed_events(), _NoAux())  # type: ignore[arg-type]
    try:
        assert isinstance(await waiter.wait(None), DrainClosedWake)
        assert isinstance(await waiter.wait(0.001), DrainTimeoutWake)
    finally:
        await waiter.close()


class _Receiver:
    primary_event_scope = None

    def __init__(self, events: list[RawHarnessEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        for event in self._events:
            yield event

    def observe_event_semantics(self, semantics: object) -> None:
        _ = semantics


class _Coordinator:
    def __init__(self, calls: list[Call]) -> None:
        self._calls = calls

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def next_timeout(self) -> None:
        return None

    async def observe_event(self, event: RawHarnessEvent, transition: str | None) -> bool:
        _ = transition
        self._calls.append(("pre_persist", event))
        return False

    def note_event_persisted(self, event: RawHarnessEvent) -> DrainLoopDecision:
        self._calls.append(("noted", event))
        return DrainLoopDecision()

    async def handle_terminal_event(
        self,
        event: RawHarnessEvent,
        outcome: Any,
        action: Any,
    ) -> DrainTerminalDecision:
        del action
        self._calls.append(("terminal", event))
        return DrainTerminalDecision(recorded_outcome=outcome)

    async def after_event(self) -> DrainLoopDecision:
        return DrainLoopDecision()

    def handle_close(self, *, intentional_stop: bool) -> None:
        _ = intentional_stop
        return None

    async def handle_stream_exit(self, recorded_outcome: Any) -> DrainExitDecision:
        return DrainExitDecision(recorded_outcome=recorded_outcome)


class _ConcurrentWakeReceiver:
    primary_event_scope = None

    def __init__(self, wake: asyncio.Event) -> None:
        self._wake = wake

    def observe_event_semantics(self, semantics: object) -> None:
        _ = semantics

    async def events(self) -> AsyncIterator[RawHarnessEvent]:
        yield RawHarnessEvent(event_type="turn/completed", harness_id="codex", payload={})
        await self._wake.wait()
        yield RawHarnessEvent(event_type="message", harness_id="codex", payload={})


class _StabilizingEvidence:
    def __init__(self, wake: asyncio.Event, candidate_started: asyncio.Event) -> None:
        self._wake = wake
        self._candidate_started = candidate_started
        self.aux_waiting = asyncio.Event()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def observe_event(
        self,
        event: RawHarnessEvent,
        transition: str | None,
    ) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_persisted(self, event: RawHarnessEvent) -> EvidenceEventDecision:
        if event.event_type == "message":
            return EvidenceEventDecision(activity=EvidenceActivity(code="persisted_event"))
        return EvidenceEventDecision()

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        del trigger
        return WorkAssessment(disposition="ready", blockers=(), generation=1)

    def next_due_at(self) -> float | None:
        return None

    async def handle_due(self) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return self._candidate_started.is_set()

    async def wait_for_change(self) -> None:
        self.aux_waiting.set()
        await self._wake.wait()

    def request_validation(self) -> int:
        return 1

    def validation_complete(self, request: int) -> bool:
        return request == 1


class _StabilizingProfile:
    def __init__(self, candidate_started: asyncio.Event) -> None:
        self._candidate_started = candidate_started
        self.evaluations: list[CompletionEvaluation] = []

    def allows_evaluation_without_candidate(self) -> bool:
        return False

    def consume_directives(
        self,
        state: CompletionState,
        trigger: AssessmentTrigger,
    ) -> CompletionDirectives:
        del state, trigger
        return CompletionDirectives()

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision:
        self.evaluations.append(context)
        candidate = context.candidate or context.terminal_outcome
        assert candidate is not None
        if context.terminal_outcome is not None:
            self._candidate_started.set()
        if context.state.phase == "stabilizing":
            if context.evidence_activity is not None:
                return ProfileDecision(action="stabilize", restart_stabilization=True)
            if context.stabilization_elapsed:
                return ProfileDecision(action="complete", outcome=candidate)
        return ProfileDecision(action="stabilize")

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None:
        del decision, now
        return None

    def stabilization_seconds(self) -> float:
        return 2.0

    def close_outcome(
        self,
        state: CompletionState,
        intentional_stop: bool,
    ) -> None:
        del state, intentional_stop
        return None

    def next_nudge_at(
        self,
        state: CompletionState,
        assessment: WorkAssessment,
    ) -> float | None:
        del state, assessment
        return None

    async def send_nudge(self, urgency: NudgeUrgency) -> None:
        del urgency

    def stream_exit_decision(
        self,
        state: CompletionState,
        recorded_outcome: Any,
    ) -> ProfileExitDecision:
        del state
        return ProfileExitDecision(recorded_outcome=recorded_outcome)


class _NoopCompletionCleanup:
    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        del assessment, reason
        return CleanupReport()


async def _run_drain(
    events: list[RawHarnessEvent],
    *,
    outcomes: list[DrainOutcome] | None = None,
) -> list[Call]:
    calls: list[Call] = []

    def emit_event(_spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        calls.append(("hooks", event))

    sessions: dict[SpawnId, SpawnSession] = {}
    if outcomes is not None:
        sessions[_SPAWN_ID] = cast(
            "SpawnSession",
            SimpleNamespace(
                cancel_sent=False,
                started_monotonic=0.0,
                subscriber=None,
                authoritative_stop_outcome=None,
            ),
        )

    def _publish_terminal(
        _spawn_id: SpawnId,
        _session: SpawnSession,
        outcome: DrainOutcome,
        _cleanup_request: object,
    ) -> DrainOutcome:
        assert outcomes is not None
        outcomes.append(outcome)
        return outcome

    loop = SpawnDrainLoop(
        sessions=sessions,
        emit_event=emit_event,
        publish_terminal=_publish_terminal if outcomes is not None else Mock(),
        fan_out_event=lambda _spawn_id, event: calls.append(("fan_out", event.raw)),
        fan_out_turn_boundary=AsyncMock(),
    )
    coordinator = cast("DrainCoordinator", _Coordinator(calls))

    await loop.run(
        spawn_id=_SPAWN_ID,
        receiver=cast("HarnessConnection[Any]", _Receiver(events)),
        drain_plan=DrainPlan(coordinator=coordinator),
        tracer=None,
    )
    return calls


@pytest.mark.asyncio
async def test_each_event_runs_hooks_then_fan_out_then_coordinator_note() -> None:
    events = [
        RawHarnessEvent(event_type="message", harness_id="test", payload={"id": index})
        for index in range(2)
    ]

    calls = await _run_drain(events)

    assert calls == [
        call
        for event in events
        for call in (
            ("pre_persist", event),
            ("hooks", event),
            ("fan_out", event),
            ("noted", event),
        )
    ]


@pytest.mark.asyncio
async def test_held_descendant_refresh_does_not_block_ordered_event_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[Call] = []
    events = [
        RawHarnessEvent(event_type="message", harness_id="pi", payload={"id": index})
        for index in range(3)
    ]

    projection = descendant_evidence_module.HistoryIndex.descendant_projection

    def held_projection(index: object, root_spawn_id: str) -> object:
        entered.set()
        release.wait(timeout=5)
        return projection(index, root_spawn_id)  # type: ignore[arg-type]

    monkeypatch.setattr(
        descendant_evidence_module.HistoryIndex,
        "descendant_projection",
        held_projection,
    )
    started = await PiDrainScenario.start(tmp_path, monkeypatch, spawn_id=_SPAWN_ID)

    def emit_event(_spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        calls.append(("hooks", event))

    loop = SpawnDrainLoop(
        sessions={},
        emit_event=emit_event,
        publish_terminal=Mock(),
        fan_out_event=lambda _spawn_id, event: calls.append(("fan_out", event.raw)),
        fan_out_turn_boundary=AsyncMock(),
    )
    try:
        run_task = asyncio.create_task(
            loop.run(
                spawn_id=_SPAWN_ID,
                receiver=cast("HarnessConnection[Any]", _Receiver(events)),
                drain_plan=DrainPlan(coordinator=cast("DrainCoordinator", started.coordinator)),
                tracer=None,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(run_task, timeout=1)
    finally:
        release.set()
        await started.stop()

    assert [call[0] for call in calls].count("hooks") == len(events)
    assert [call[0] for call in calls].count("fan_out") == len(events)


@pytest.mark.asyncio
async def test_codex_interrupted_turn_publishes_cancelled_drain_outcome() -> None:
    interrupted = RawHarnessEvent(
        event_type="turn/completed",
        harness_id="codex",
        payload={"turn": {"status": "interrupted"}},
    )
    outcomes: list[DrainOutcome] = []

    await _run_drain([interrupted], outcomes=outcomes)

    assert len(outcomes) == 1
    assert outcomes[0].status == "cancelled"
    assert outcomes[0].exit_code == 130
    assert outcomes[0].error == "interrupted"


@pytest.mark.asyncio
async def test_persisted_activity_restarts_elapsed_stabilization_before_concurrent_aux() -> None:
    wake = asyncio.Event()
    candidate_started = asyncio.Event()
    clock = FakeClock()
    evidence = _StabilizingEvidence(wake, candidate_started)
    profile = _StabilizingProfile(candidate_started)
    coordinator = CompletionCoordinator(
        evidence=evidence,
        profile=profile,
        cleanup=_NoopCompletionCleanup(),
        clock=clock.monotonic,
    )
    loop = SpawnDrainLoop(
        sessions={},
        emit_event=lambda _spawn_id, event: None,
        publish_terminal=Mock(),
        fan_out_event=Mock(),
        fan_out_turn_boundary=AsyncMock(),
    )
    run_task = asyncio.create_task(
        loop.run(
            spawn_id=_SPAWN_ID,
            receiver=cast("HarnessConnection[Any]", _ConcurrentWakeReceiver(wake)),
            drain_plan=DrainPlan(
                coordinator=coordinator,
                aux_wake=coordinator,
                handle_aux_wake=coordinator.handle_aux_wake,
            ),
            tracer=None,
        )
    )
    await asyncio.wait_for(evidence.aux_waiting.wait(), timeout=1.0)

    clock.advance(2.0)
    wake.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    aux_evaluation = next(
        context for context in profile.evaluations if context.trigger == "aux_wake"
    )
    assert aux_evaluation.stabilization_elapsed is False
    assert aux_evaluation.evidence_activity == EvidenceActivity(code="persisted_event")
    after_event_evaluation = profile.evaluations[-1]
    assert after_event_evaluation.trigger == "event"
    assert after_event_evaluation.stabilization_elapsed is False
    assert after_event_evaluation.state.stabilization_at == 4.0
