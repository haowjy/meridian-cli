"""Spawn drain persistence-ordering regression tests."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
from meridian.lib.state.history import HarnessHistoryWriter, WriteResult, read_history_range
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
)
from meridian.lib.streaming.drain_wait import _cancel_task
from meridian.lib.streaming.event_observers import EventObserverRegistry
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession
from tests.support.fakes import FakeClock

_SPAWN_ID = SpawnId("p-persist-order")
Call = tuple[str, HarnessEvent]


def test_history_writer_stamps_lifecycle_rows_with_subsecond_wall_clock(tmp_path: Path) -> None:
    class _SubsecondClock(FakeClock):
        def utc_now_iso(self) -> str:
            return "2026-07-17T13:14:15.123Z"

    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path, clock=_SubsecondClock())

    result = writer.write(
        HarnessEvent(
            event_type="meridian.pi.lifecycle.phase",
            harness_id="pi",
            payload={"phase": "finalized"},
        )
    )

    assert result.success is True
    assert read_history_range(history_path)[0]["timestamp"] == "2026-07-17T13:14:15.123Z"


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


class _Receiver:
    primary_event_scope = None

    def __init__(self, events: list[HarnessEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[HarnessEvent]:
        for event in self._events:
            yield event


class _HistoryWriter:
    def __init__(self, results: list[WriteResult], calls: list[Call]) -> None:
        self._results = iter(results)
        self._calls = calls

    def write(self, event: HarnessEvent) -> WriteResult:
        self._calls.append(("persist", event))
        return next(self._results)


class _Coordinator:
    def __init__(self, calls: list[Call]) -> None:
        self._calls = calls

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def next_timeout(self) -> None:
        return None

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        _ = transition
        self._calls.append(("pre_persist", event))
        return False

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        self._calls.append(("noted", event))
        return DrainLoopDecision()

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

    async def events(self) -> AsyncIterator[HarnessEvent]:
        yield HarnessEvent(event_type="turn/completed", harness_id="codex", payload={})
        await self._wake.wait()
        yield HarnessEvent(event_type="message", harness_id="codex", payload={})


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
        event: HarnessEvent,
        transition: str | None,
    ) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision:
        if event.event_type == "message":
            return EvidenceEventDecision(
                activity=EvidenceActivity(code="persisted_event")
            )
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
    events: list[HarnessEvent],
    results: list[WriteResult],
    *,
    outcomes: list[DrainOutcome] | None = None,
) -> list[Call]:
    calls: list[Call] = []

    def record_observer_dispatch(_spawn_id: SpawnId, event: HarnessEvent) -> None:
        calls.append(("observe", event))

    observers = Mock()
    observers.dispatch.side_effect = record_observer_dispatch
    sessions: dict[SpawnId, SpawnSession] = {}
    if outcomes is not None:
        sessions[_SPAWN_ID] = cast(
            "SpawnSession",
            SimpleNamespace(
                cancel_sent=False,
                started_monotonic=0.0,
                subscriber=None,
                preferred_stop_outcome=None,
            ),
        )

    def _resolve_outcome(_session: SpawnSession, outcome: DrainOutcome) -> DrainOutcome:
        assert outcomes is not None
        outcomes.append(outcome)
        return outcome

    loop = SpawnDrainLoop(
        sessions=sessions,
        history_writers={
            _SPAWN_ID: cast("HarnessHistoryWriter", _HistoryWriter(results, calls))
        },
        observers=cast("EventObserverRegistry", observers),
        cleanup_tasks=set(),
        cleanup_completed_session=AsyncMock(),
        resolve_completion_future=_resolve_outcome if outcomes is not None else Mock(),
        fan_out_event=lambda _spawn_id, event: calls.append(("fan_out", event)),
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
async def test_failed_history_write_blocks_post_persist_delivery() -> None:
    failed_event = HarnessEvent(event_type="message", harness_id="test", payload={"id": 1})
    persisted_event = HarnessEvent(
        event_type="message",
        harness_id="test",
        payload={"id": 2},
    )

    calls = await _run_drain(
        [failed_event, persisted_event],
        [
            WriteResult(success=False, error="transient write failure"),
            WriteResult(success=True, seq=0),
        ],
    )

    assert calls == [
        ("pre_persist", failed_event),
        ("persist", failed_event),
        ("pre_persist", persisted_event),
        ("persist", persisted_event),
        ("observe", persisted_event),
        ("fan_out", persisted_event),
        ("noted", persisted_event),
    ]


@pytest.mark.asyncio
async def test_tenth_history_write_failure_aborts_without_delivery() -> None:
    events = [
        HarnessEvent(event_type="message", harness_id="test", payload={"id": index})
        for index in range(11)
    ]

    outcomes: list[DrainOutcome] = []
    calls = await _run_drain(
        events,
        [WriteResult(success=False, error="write failure") for _ in events],
        outcomes=outcomes,
    )

    assert calls == [
        call
        for event in events[:10]
        for call in (("pre_persist", event), ("persist", event))
    ]
    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].exit_code == 1
    assert outcomes[0].error == (
        "Aborted drain loop after repeated output persistence failures"
    )


@pytest.mark.asyncio
async def test_terminal_frame_history_write_failure_is_not_delivered_or_terminal() -> None:
    failed_terminal = HarnessEvent(
        event_type="agent_end",
        harness_id="pi",
        payload={"messages": [{"role": "assistant", "stopReason": "stop"}]},
    )
    persisted_after_terminal = HarnessEvent(
        event_type="message",
        harness_id="pi",
        payload={"id": "after-failed-terminal"},
    )

    calls = await _run_drain(
        [failed_terminal, persisted_after_terminal],
        [
            WriteResult(success=False, error="terminal write failure"),
            WriteResult(success=True, seq=0),
        ],
    )

    assert calls == [
        ("pre_persist", failed_terminal),
        ("persist", failed_terminal),
        ("pre_persist", persisted_after_terminal),
        ("persist", persisted_after_terminal),
        ("observe", persisted_after_terminal),
        ("fan_out", persisted_after_terminal),
        ("noted", persisted_after_terminal),
    ]


@pytest.mark.asyncio
async def test_persisted_activity_restarts_elapsed_stabilization_before_concurrent_aux(
) -> None:
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
    history_writer = _HistoryWriter(
        [WriteResult(success=True, seq=0), WriteResult(success=True, seq=1)],
        [],
    )
    observers = Mock()
    loop = SpawnDrainLoop(
        sessions={},
        history_writers={_SPAWN_ID: cast("HarnessHistoryWriter", history_writer)},
        observers=cast("EventObserverRegistry", observers),
        cleanup_tasks=set(),
        cleanup_completed_session=AsyncMock(),
        resolve_completion_future=Mock(),
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
