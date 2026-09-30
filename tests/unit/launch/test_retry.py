"""Truth table for fail-closed startup recovery."""

from __future__ import annotations

import itertools

import pytest

from meridian.lib.core.native_identity import NativeCreateProgress
from meridian.lib.harness.connections.errors import TurnSubmission
from meridian.lib.launch.retry import (
    AttemptFailure,
    FailureDisposition,
    ReplayEvidence,
    ReplaySafety,
    decide_retry,
)


@pytest.mark.parametrize(
    ("disposition", "turn", "native_create", "attempts_used", "expected"),
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
            and case[3] == 1,
        )
        for case in itertools.product(
            FailureDisposition,
            TurnSubmission,
            NativeCreateProgress,
            (1, 2),
        )
    ],
)
def test_retry_requires_transient_safe_evidence_and_remaining_attempt(
    disposition: FailureDisposition,
    turn: TurnSubmission,
    native_create: NativeCreateProgress,
    attempts_used: int,
    expected: bool,
) -> None:
    decision = decide_retry(
        AttemptFailure(disposition, "test_failure"),
        ReplayEvidence(turn, native_create),
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
        ),
        attempts_used=1,
        max_attempts=2,
    )

    assert decision.retry
