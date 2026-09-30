"""Typed, fail-closed startup recovery policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meridian.lib.core.native_identity import NativeCreateProgress
from meridian.lib.harness.connections.errors import (
    ConnectionStartFailure,
    RetryableConnectionStartupError,
    TeardownStatus,
    TurnSubmission,
)
from meridian.lib.harness.semantics import TerminalEventOutcome, TerminalOutcomeCause
from meridian.lib.launch.errors import ErrorCategory


class FailureDisposition(StrEnum):
    TRANSIENT = "transient"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ReplaySafety(StrEnum):
    PROVEN_SAFE = "proven_safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AttemptFailure:
    disposition: FailureDisposition
    code: str
    message: str | None = None

    @property
    def final_message(self) -> str:
        """Stable report cause selected by the same record used for retry."""

        return self.message or self.code


@dataclass(frozen=True)
class ReplayEvidence:
    turn: TurnSubmission
    native_create: NativeCreateProgress
    teardown: TeardownStatus


@dataclass(frozen=True)
class RetryAssessment:
    failure: AttemptFailure
    evidence: ReplayEvidence
    replay_safety: ReplaySafety
    reason: str


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    assessment: RetryAssessment


@dataclass(frozen=True)
class RetryPermit:
    """Capability consumed by NativeRun when rearming a proven-safe identity."""

    decision: RetryDecision

    def __post_init__(self) -> None:
        if not self.decision.retry:
            raise ValueError("retry permits require an affirmative retry decision")


def classify_attempt_failure(
    *,
    cancelled: bool,
    cancellation_message: str | None,
    terminal_outcome: TerminalEventOutcome | None,
    terminal_code: str | None,
    terminal_message: str | None,
    start_failure: ConnectionStartFailure | None,
    guardrail_failed: bool,
    timed_out: bool,
    budget_exceeded: bool,
    legacy_category: ErrorCategory,
    fallback_message: str | None,
) -> AttemptFailure:
    """Select the strongest typed cause; weaker diagnostics cannot override it."""

    if cancelled:
        return AttemptFailure(
            FailureDisposition.CANCELLED,
            "cancelled",
            cancellation_message or "cancelled",
        )
    if terminal_outcome is not None and terminal_outcome.exit_code != 0:
        disposition = (
            FailureDisposition.TRANSIENT
            if terminal_outcome.cause is TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE
            else FailureDisposition.TERMINAL
        )
        return AttemptFailure(disposition, "harness_terminal", terminal_outcome.error)
    if terminal_code is not None:
        return AttemptFailure(
            FailureDisposition.TERMINAL,
            terminal_code,
            terminal_message or terminal_code,
        )
    if guardrail_failed:
        return AttemptFailure(FailureDisposition.TERMINAL, "guardrail_failed")
    if timed_out:
        return AttemptFailure(FailureDisposition.TERMINAL, "timeout")
    if budget_exceeded:
        return AttemptFailure(FailureDisposition.TERMINAL, "budget_exceeded")
    if start_failure is not None:
        disposition = (
            FailureDisposition.TRANSIENT
            if isinstance(start_failure.cause, RetryableConnectionStartupError)
            else FailureDisposition.UNKNOWN
        )
        return AttemptFailure(
            disposition,
            type(start_failure.cause).__name__,
            str(start_failure.cause),
        )
    if legacy_category is ErrorCategory.RETRYABLE:
        return AttemptFailure(
            FailureDisposition.TRANSIENT,
            "legacy_transient",
            fallback_message,
        )
    if legacy_category is ErrorCategory.STRATEGY_CHANGE:
        return AttemptFailure(FailureDisposition.TERMINAL, "strategy_change")
    return AttemptFailure(
        FailureDisposition.UNKNOWN,
        "unclassified_failure",
        fallback_message,
    )


def assess_replay_safety(evidence: ReplayEvidence) -> tuple[ReplaySafety, str]:
    """Reduce typed transport/identity observations without guessing."""

    if evidence.turn is TurnSubmission.SUBMITTED:
        return ReplaySafety.UNSAFE, "initial_turn_submitted"
    if evidence.native_create is NativeCreateProgress.MATERIALIZED:
        return ReplaySafety.UNSAFE, "native_create_materialized"
    if evidence.teardown is not TeardownStatus.QUIESCENT:
        return ReplaySafety.UNKNOWN, "teardown_not_quiescent"
    if evidence.turn is TurnSubmission.NOT_SUBMITTED and evidence.native_create in {
        NativeCreateProgress.NOT_MATERIALIZED,
        NativeCreateProgress.NOT_APPLICABLE,
    }:
        return ReplaySafety.PROVEN_SAFE, "turn_unsubmitted_and_identity_unconsumed"
    return ReplaySafety.UNKNOWN, "replay_progress_unknown"


def decide_retry(
    failure: AttemptFailure,
    evidence: ReplayEvidence,
    *,
    attempts_used: int,
    max_attempts: int,
) -> RetryDecision:
    """Permit only bounded transient recovery with affirmative replay proof."""

    replay_safety, replay_reason = assess_replay_safety(evidence)
    attempts_remain = attempts_used < max_attempts
    retry = (
        failure.disposition is FailureDisposition.TRANSIENT
        and replay_safety is ReplaySafety.PROVEN_SAFE
        and attempts_remain
    )
    if failure.disposition is not FailureDisposition.TRANSIENT:
        reason = f"failure_{failure.disposition.value}"
    elif replay_safety is not ReplaySafety.PROVEN_SAFE:
        reason = replay_reason
    elif not attempts_remain:
        reason = "attempts_exhausted"
    else:
        reason = "safe_startup_recovery"
    return RetryDecision(
        retry=retry,
        assessment=RetryAssessment(
            failure=failure,
            evidence=evidence,
            replay_safety=replay_safety,
            reason=reason,
        ),
    )


__all__ = [
    "AttemptFailure",
    "FailureDisposition",
    "ReplayEvidence",
    "ReplaySafety",
    "RetryAssessment",
    "RetryDecision",
    "RetryPermit",
    "assess_replay_safety",
    "classify_attempt_failure",
    "decide_retry",
]
