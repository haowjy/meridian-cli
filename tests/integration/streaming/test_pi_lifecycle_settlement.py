"""Pi session lifecycle regressions at the harness/streaming seam."""

from __future__ import annotations

import asyncio
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
from meridian.lib.streaming.drain_policy import (
    PersistentDrainPolicy,
    PiRpcQuiescenceDrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.pi_completion_profile import (
    PiCompletionProfile,
    PiOutstandingWork,
)
from tests.support.pi import PiDrainScenario, pi_event


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
        "summarization_retry_scheduled",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
        "queue_update",
    ],
)
def test_pi_automatic_work_events_keep_parent_active(event_type: str) -> None:
    semantics = normalize_event(pi_event(event_type)).semantics

    assert semantics.activity == "turn_active"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"reason": "manual"}, {"reason": "overflow"}, {}])
async def test_compaction_end_cannot_settle_an_active_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch)
    try:
        await started.observe("agent_start", {})
        await started.observe("compaction_start", {})
        await started.observe("compaction_end", payload)
        assert not started.coordinator._profile.quiescence_tracker.parent_idle
    finally:
        await started.stop()


def test_pi_settlement_success_is_private_until_session_refinement() -> None:
    semantics = normalize_event(pi_event("agent_settled", {"aborted": False})).semantics

    assert semantics.terminal is None


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
    try:
        first_refined = await coordinator.observe_event(normalize_event(first))
        assert first_refined is not None and first_refined.semantics.terminal is None

        settled = pi_event("agent_settled", {"aborted": False})
        settled_refined = await coordinator.observe_event(normalize_event(settled))
        assert settled_refined is not None and settled_refined.semantics.terminal is not None
        outcome = settled_refined.semantics.terminal
        final = await coordinator.handle_terminal_event(
            settled,
            outcome,
            PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent).classify(
                outcome
            ),
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
    try:
        attempt_refined = await coordinator.observe_event(normalize_event(attempt))
        assert attempt_refined is not None and attempt_refined.semantics.terminal is None

        settled = pi_event("agent_settled", {"aborted": True})
        settled_refined = await coordinator.observe_event(normalize_event(settled))
        assert settled_refined is not None and settled_refined.semantics.terminal is not None
        outcome = settled_refined.semantics.terminal
        final = await coordinator.handle_terminal_event(
            settled,
            outcome,
            PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent).classify(
                outcome
            ),
        )

        assert final.recorded_outcome is not None
        assert final.recorded_outcome.status == "cancelled"
        assert final.recorded_outcome.exit_code == 130
    finally:
        await started.stop()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user"}],
        [{"role": "assistant"}],
        [{"role": "assistant", "stopReason": "invented"}],
    ],
)
def test_attempt_requires_an_assistant_outcome(messages: list[dict[str, object]]) -> None:
    outcome = normalize_event(pi_event("agent_end", {"messages": messages})).semantics.terminal
    assert outcome is not None and outcome.status == "failed"


def test_final_provider_diagnostic_is_preserved() -> None:
    message = {
        "role": "assistant",
        "stopReason": "error",
        "errorMessage": "503 SPECIFIC_PROVIDER_FAILURE",
    }
    outcome = normalize_event(pi_event("agent_end", {"messages": [message]})).semantics.terminal
    assert outcome is not None and "SPECIFIC_PROVIDER_FAILURE" in (outcome.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [SingleTurnDrainPolicy(), PersistentDrainPolicy()])
async def test_explicit_policy_survives_settlement_with_owed_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: SingleTurnDrainPolicy | PersistentDrainPolicy,
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch)
    coordinator = started.coordinator
    coordinator.set_policy(policy)
    started.running_bash()
    try:
        for event in [
            pi_event("agent_end", {"messages": [{"role": "assistant", "stopReason": "stop"}]}),
            pi_event("agent_settled", {"aborted": False}),
        ]:
            refined = await coordinator.observe_event(normalize_event(event))
            if refined is None or refined.semantics.terminal is None:
                continue
            outcome = refined.semantics.terminal
            result = await coordinator.handle_terminal_event(
                event,
                outcome,
                policy.classify(outcome),
            )
        if isinstance(policy, SingleTurnDrainPolicy):
            assert result.recorded_outcome is not None
            assert result.recorded_outcome.status == "succeeded"
        else:
            assert result.recorded_outcome is None and result.emit_turn_boundary
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_new_run_discards_previous_close_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch, start_micro_drain=True)
    try:
        assert started.coordinator.handle_close(intentional_stop=False) is not None
        await started.observe("agent_start")
        assert started.coordinator.handle_close(intentional_stop=False) is None
        assert not started.coordinator.should_defer_close()
    finally:
        await started.stop()


@pytest.mark.parametrize("payload", [{}, {"aborted": "false"}, {"aborted": 0}])
def test_invalid_settlement_cannot_refine_a_retained_attempt(payload: dict[str, object]) -> None:
    outcome = normalize_event(pi_event("agent_settled", payload)).semantics.terminal
    assert outcome is not None and outcome.error == "pi_invalid_agent_settled"
    assert outcome.cause is None


def test_empty_queue_update_does_not_start_new_work() -> None:
    event = pi_event("queue_update", {"steering": [], "followUp": []})
    assert normalize_event(event).semantics.activity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_first", [True, False])
async def test_compaction_and_settlement_must_both_finish_before_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settled_first: bool,
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch)
    try:
        events = [
            pi_event("agent_settled", {"aborted": False}),
            pi_event("compaction_start", {"reason": "manual"}),
        ]
        if not settled_first:
            events.reverse()
        events += [
            pi_event(name)
            for name in [
                "summarization_retry_scheduled",
                "summarization_retry_attempt_start",
                "summarization_retry_finished",
            ]
        ]
        for event in events:
            await started.coordinator.observe_event(normalize_event(event))
        tracker = started.coordinator._profile.quiescence_tracker
        assert not tracker.parent_idle
        await started.observe("compaction_end", {"reason": "manual"})
        assert tracker.parent_idle
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_done_can_release_work_after_manual_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = await PiDrainScenario.start(tmp_path, monkeypatch, patch_clock=False)
    coordinator = started.coordinator
    try:
        started.row("p1", parent_id=None)
        started.running_bash()
        await started.idle()
        await coordinator.handle_aux_wake()
        await started.terminal()
        for kind, _transition in [("compaction_start", "turn_active"), ("compaction_end", "idle")]:
            event = pi_event(kind, {"reason": "manual"})
            await coordinator.observe_event(normalize_event(event))
            coordinator.note_event_delivered(event)
            await coordinator.after_event()
        started.done()
        outcome = None
        for _ in range(100):
            await asyncio.sleep(0.025)
            outcome = (await started.timeout()).recorded_outcome
            if outcome is not None:
                break
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


def test_delivery_deadline_rearms_after_active_run(tmp_path: Path) -> None:
    now = [0.0]
    tracker = SimpleNamespace(parent_idle=True)
    blocker = DiagnosticBlocker(source="profile", code="pi_result_delivery_pending", identity="b1")
    assessment = WorkAssessment(disposition="blocked", blockers=(blocker,), generation=1)
    evidence = SimpleNamespace(
        quiescence_tracker=tracker,
        session_seen=False,
        session_phase_emitted=False,
        has_pending_children=lambda: False,
        pending_child_count=lambda: 0,
        classify_outstanding_work=lambda: PiOutstandingWork(False, False, delivery_pending=True),
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
