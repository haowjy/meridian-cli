"""Truth table for fail-closed startup recovery."""

from __future__ import annotations

import itertools

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.native_identity import NativeCreateProgress
from meridian.lib.harness.connections.errors import TeardownStatus, TurnSubmission
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.errors import ErrorCategory
from meridian.lib.launch.retry import (
    AttemptFailure,
    FailureDisposition,
    ReplayEvidence,
    ReplaySafety,
    classify_attempt_failure,
    decide_retry,
)


@pytest.mark.parametrize(
    ("disposition", "turn", "native_create", "teardown", "attempts_used", "expected"),
    [
        (
            *case,
            case[0] is FailureDisposition.TRANSIENT
            and case[1] is TurnSubmission.NOT_SUBMITTED
            and case[2]
            in {
                NativeCreateProgress.NOT_MATERIALIZED,
                NativeCreateProgress.NOT_APPLICABLE,
            }
            and case[3] is TeardownStatus.QUIESCENT
            and case[4] == 1,
        )
        for case in itertools.product(
            FailureDisposition,
            TurnSubmission,
            NativeCreateProgress,
            TeardownStatus,
            (1, 2),
        )
    ],
)
def test_retry_requires_transient_safe_evidence_and_remaining_attempt(
    disposition: FailureDisposition,
    turn: TurnSubmission,
    native_create: NativeCreateProgress,
    teardown: TeardownStatus,
    attempts_used: int,
    expected: bool,
) -> None:
    decision = decide_retry(
        AttemptFailure(disposition, "test_failure"),
        ReplayEvidence(turn, native_create, teardown),
        attempts_used=attempts_used,
        max_attempts=2,
    )

    assert decision.retry is expected
    if expected:
        assert decision.assessment.replay_safety is ReplaySafety.PROVEN_SAFE


def test_not_applicable_native_identity_can_be_proven_safe() -> None:
    decision = decide_retry(
        AttemptFailure(FailureDisposition.TRANSIENT, "typed_transport_failure"),
        ReplayEvidence(
            TurnSubmission.NOT_SUBMITTED,
            NativeCreateProgress.NOT_APPLICABLE,
            TeardownStatus.QUIESCENT,
        ),
        attempts_used=1,
        max_attempts=2,
    )

    assert decision.retry


def test_explicit_terminal_record_owns_final_message_over_fallbacks() -> None:
    failure = classify_attempt_failure(
        cancelled=False,
        cancellation_message=None,
        terminal_outcome=TerminalEventOutcome(
            status=SpawnStatus.FAILED,
            exit_code=1,
            error="prompt too long: reduce input",
        ),
        terminal_code="entry_mismatch",
        terminal_message="entry_mismatch",
        start_failure=None,
        guardrail_failed=False,
        timed_out=False,
        budget_exceeded=True,
        legacy_category=ErrorCategory.STRATEGY_CHANGE,
        fallback_message="strategy_change",
    )

    assert failure.disposition is FailureDisposition.TERMINAL
    assert failure.final_message == "prompt too long: reduce input"
