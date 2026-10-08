"""Pi session lifecycle regressions at the harness/streaming seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.semantics import normalize_event
from meridian.lib.streaming.completion_contracts import (
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    WorkAssessment,
)
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_completion_profile import (
    PiCompletionProfile,
    PiOutstandingWork,
)
from tests.support.pi import PiDrainScenario, pi_event

_SUCCESS_ACTION = DrainAction(terminate=True, emit_turn_boundary=False)


def _retryable_error() -> object:
    return {
        "messages": [
            {
                "role": "assistant",
                "stopReason": "error",
            }
        ],
        "willRetry": True,
    }


@pytest.mark.parametrize("event_type", ["turn_end", "agent_end"])
def test_pi_batch_events_are_not_session_idle(event_type: str) -> None:
    payload = _retryable_error() if event_type == "agent_end" else {}
    semantics = normalize_event(pi_event(event_type, payload)).semantics

    assert semantics.activity != "idle"


@pytest.mark.parametrize(
    "event_type",
    [
        "auto_retry_start",
        "auto_retry_end",
        "compaction_start",
        "compaction_end",
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
        "queue_update",
    ],
)
def test_pi_automatic_work_events_keep_parent_active(event_type: str) -> None:
    semantics = normalize_event(pi_event(event_type)).semantics

    assert semantics.activity == "turn_active"


@pytest.mark.parametrize(
    ("payload", "activity"),
    [
        ({"reason": "manual", "aborted": False}, "idle"),
        ({"reason": "overflow", "willRetry": True}, "turn_active"),
        ({}, "turn_active"),
    ],
)
def test_pi_compaction_end_only_settles_explicit_manual_work(
    payload: dict[str, object], activity: str
) -> None:
    semantics = normalize_event(pi_event("compaction_end", payload)).semantics

    assert semantics.activity == activity


def test_pi_settled_without_attempt_is_not_synthetic_success() -> None:
    semantics = normalize_event(pi_event("agent_settled", {"aborted": False})).semantics

    assert semantics.terminal is not None
    assert semantics.terminal.status == "failed"


def test_pi_malformed_attempt_is_not_synthetic_success() -> None:
    semantics = normalize_event(pi_event("agent_end")).semantics

    assert semantics.terminal is not None
    assert semantics.terminal.status == "failed"
    assert semantics.terminal.error == "pi_agent_end_missing_messages"


@pytest.mark.asyncio
async def test_retryable_agent_end_waits_for_settlement_and_preserves_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch)
    coordinator = started.coordinator
    first = pi_event("agent_end", _retryable_error())
    first_semantics = normalize_event(first).semantics
    assert first_semantics.terminal is not None
    try:
        await coordinator.observe_event(first, first_semantics.activity)
        action = PiRpcQuiescenceDrainPolicy(
            quiescence_check=coordinator.is_quiescent
        ).classify(first_semantics.terminal)
        provisional = await coordinator.handle_terminal_event(
            first, first_semantics.terminal, action
        )
        assert provisional.recorded_outcome is None

        settled = pi_event("agent_settled", {"aborted": False})
        settled_semantics = normalize_event(settled).semantics
        assert settled_semantics.terminal is not None
        await coordinator.observe_event(settled, settled_semantics.activity)
        final = await coordinator.handle_terminal_event(
            settled,
            settled_semantics.terminal,
            _SUCCESS_ACTION,
        )

        assert final.recorded_outcome is not None
        assert final.recorded_outcome.status == "failed"
        assert final.recorded_outcome.error == "pi_stop_error"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_settled_abort_remains_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch)
    coordinator = started.coordinator
    attempt = pi_event(
        "agent_end",
        {
            "messages": [
                {"role": "assistant", "stopReason": "stop"},
            ],
            "willRetry": False,
        },
    )
    attempt_semantics = normalize_event(attempt).semantics
    assert attempt_semantics.terminal is not None
    try:
        await coordinator.observe_event(attempt, attempt_semantics.activity)
        await coordinator.handle_terminal_event(
            attempt,
            attempt_semantics.terminal,
            PiRpcQuiescenceDrainPolicy(
                quiescence_check=coordinator.is_quiescent
            ).classify(attempt_semantics.terminal),
        )

        settled = pi_event("agent_settled", {"aborted": True})
        settled_semantics = normalize_event(settled).semantics
        assert settled_semantics.terminal is not None
        await coordinator.observe_event(settled, settled_semantics.activity)
        final = await coordinator.handle_terminal_event(
            settled,
            settled_semantics.terminal,
            _SUCCESS_ACTION,
        )

        assert final.recorded_outcome is not None
        assert final.recorded_outcome.status == "cancelled"
        assert final.recorded_outcome.exit_code == 130
    finally:
        await started.stop()


def test_delivery_deadline_rearms_after_active_run(tmp_path: Path) -> None:
    now = [0.0]
    tracker = SimpleNamespace(parent_idle=True)
    blocker = DiagnosticBlocker(
        source="profile", code="pi_result_delivery_pending", identity="b1"
    )
    assessment = WorkAssessment(disposition="blocked", blockers=(blocker,), generation=1)
    evidence = SimpleNamespace(
        quiescence_tracker=tracker,
        session_seen=False,
        session_phase_emitted=False,
        has_pending_children=lambda: False,
        pending_child_count=lambda: 0,
        classify_outstanding_work=lambda: PiOutstandingWork(
            False, False, delivery_pending=True
        ),
    )
    profile = PiCompletionProfile(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        session_role="spawned",
        child_wave_timeout_seconds=300.0,
        emit_phase=lambda **_payload: None,
        send_done_nudge=None,
        evidence=evidence,
        stabilization_seconds=0.05,
        clock=lambda: now[0],
    )
    context = CompletionEvaluation(
        state=CompletionState("waiting", None, assessment, None, None, None),
        trigger="event",
        now=0.0,
        directives=CompletionDirectives(),
        assessment=assessment,
        active_turn=False,
    )

    assert profile.evaluate(context).action == "wait"
    assert profile._delivery_deadline_at == 300.0

    tracker.parent_idle = False
    profile.after_observed_event("turn_active")
    now[0] = 301.0
    active_decision = profile.evaluate(
        CompletionEvaluation(
            state=CompletionState("waiting", None, assessment, None, None, None),
            trigger="event",
            now=301.0,
            directives=CompletionDirectives(),
            assessment=assessment,
            active_turn=True,
        )
    )
    assert active_decision.action == "wait"
    assert profile._delivery_deadline_at is None

    tracker.parent_idle = True
    profile.after_observed_event("idle")
    decision = profile.evaluate(
        CompletionEvaluation(
            state=CompletionState("waiting", None, assessment, None, None, None),
            trigger="event",
            now=301.0,
            directives=CompletionDirectives(),
            assessment=assessment,
            active_turn=False,
        )
    )

    assert decision.action == "wait"
    assert profile._delivery_deadline_at == 601.0
