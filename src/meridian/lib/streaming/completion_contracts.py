"""Contracts for completion-aware streaming drains."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from meridian.lib.harness.semantics import TerminalEventOutcome

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessEvent
    from meridian.lib.streaming.drain_policy import DrainAction


CompletionPhase = Literal[
    "running",
    "assessing",
    "waiting",
    "stabilizing",
    "cleaning",
    "finalized",
]
AssessmentTrigger = Literal[
    "terminal_candidate",
    "event",
    "timeout",
    "aux_wake",
    "evidence_due",
]
NudgeUrgency = Literal["normal", "timeout_soon"]
ProfileAction = Literal["clear", "complete", "wait", "stabilize", "cleanup", "fail"]


@dataclass(frozen=True)
class DiagnosticBlocker:
    """One opaque reason completion evidence is not ready."""

    source: Literal["persisted_descendant", "profile"]
    code: str
    identity: str | None = None


@dataclass(frozen=True)
class EvidenceFailure:
    """Typed evidence read/observation failure for profile interpretation."""

    code: str
    detail: str | None = None


@dataclass(frozen=True)
class WorkAssessment:
    """One fresh, generation-tagged completion evidence assessment."""

    disposition: Literal["ready", "blocked", "unknown"]
    blockers: tuple[DiagnosticBlocker, ...]
    generation: int
    failure: EvidenceFailure | None = None

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("assessment generation must be non-negative")
        if self.disposition == "ready":
            valid = not self.blockers and self.failure is None
        elif self.disposition == "blocked":
            valid = bool(self.blockers) and self.failure is None
        else:
            valid = not self.blockers and self.failure is not None
        if not valid:
            raise ValueError(f"invalid {self.disposition!r} work assessment")


@dataclass(frozen=True)
class CompletionDirectives:
    """External completion directives consumed as one fact set."""

    done: bool = False
    rearm: bool = False


@dataclass(frozen=True)
class EvidenceActivity:
    """Opaque completion-relevant activity reported by an evidence source."""

    code: str


@dataclass(frozen=True)
class EvidenceEventDecision:
    """Evidence-source response to an event or source-specific due wake."""

    duplicate_canonical_event: bool = False
    activity: EvidenceActivity | None = None
    failure: EvidenceFailure | None = None


@dataclass(frozen=True)
class CompletionState:
    """Immutable coordinator state presented to profile policy."""

    phase: CompletionPhase
    candidate: TerminalEventOutcome | None
    assessment: WorkAssessment | None
    deadline_at: float | None
    stabilization_at: float | None
    stabilization_generation: int | None


@dataclass(frozen=True)
class CompletionEvaluation:
    """All coincident facts available to one profile decision."""

    state: CompletionState
    trigger: AssessmentTrigger
    now: float
    directives: CompletionDirectives
    assessment: WorkAssessment
    deadline_expired: bool = False
    stabilization_elapsed: bool = False
    profile_timer_due: bool = False
    active_turn: bool = False
    candidate: TerminalEventOutcome | None = None
    terminal_outcome: TerminalEventOutcome | None = None
    terminal_action: DrainAction | None = None
    evidence_activity: EvidenceActivity | None = None
    evidence_failure: EvidenceFailure | None = None


@dataclass(frozen=True)
class ProfileDecision:
    """A profile-owned transition selected from a complete fact set."""

    action: ProfileAction
    outcome: TerminalEventOutcome | None = None
    emit_turn_boundary: bool = False
    cleanup_reason: str | None = None
    nudge: NudgeUrgency | None = None
    reset_deadline: bool = False
    restart_stabilization: bool = False

    def __post_init__(self) -> None:
        needs_outcome = self.action in {"complete", "cleanup", "fail"}
        if needs_outcome != (self.outcome is not None):
            raise ValueError(f"profile action {self.action!r} has an invalid outcome")
        if self.action == "cleanup" and not self.cleanup_reason:
            raise ValueError("cleanup decisions require a reason")
        if self.action != "cleanup" and self.cleanup_reason is not None:
            raise ValueError("only cleanup decisions may carry a cleanup reason")
        if self.nudge is not None and self.action != "wait":
            raise ValueError("only wait decisions may request a nudge")
        if self.restart_stabilization and self.action != "stabilize":
            raise ValueError("only stabilize decisions may restart stabilization")


@dataclass(frozen=True)
class CleanupReport:
    """Diagnostic record of one idempotent cleanup attempt."""

    attempted_categories: tuple[str, ...] = ()
    converged_cancel_ids: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    handles_attempted: tuple[str, ...] = ()
    failures: tuple[EvidenceFailure, ...] = ()


class CompletionEvidence(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def observe_event(
        self, event: HarnessEvent, transition: str | None
    ) -> EvidenceEventDecision: ...

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision: ...

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment: ...

    def next_due_at(self) -> float | None: ...

    async def handle_due(self) -> EvidenceEventDecision: ...

    def wants_aux_wake(self) -> bool: ...

    async def wait_for_change(self) -> None: ...


class CompletionProfile(Protocol):
    def consume_directives(self) -> CompletionDirectives: ...

    def evaluate(self, context: CompletionEvaluation) -> ProfileDecision: ...

    def deadline_for(self, decision: ProfileDecision, now: float) -> float | None: ...

    def stabilization_seconds(self) -> float: ...

    def close_outcome(
        self, state: CompletionState, intentional_stop: bool
    ) -> TerminalEventOutcome | None: ...

    def next_nudge_at(
        self, state: CompletionState, assessment: WorkAssessment
    ) -> float | None: ...

    async def send_nudge(self, urgency: NudgeUrgency) -> None: ...


class CompletionCleanup(Protocol):
    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport: ...


__all__ = [
    "AssessmentTrigger",
    "CleanupReport",
    "CompletionCleanup",
    "CompletionDirectives",
    "CompletionEvaluation",
    "CompletionEvidence",
    "CompletionPhase",
    "CompletionProfile",
    "CompletionState",
    "DiagnosticBlocker",
    "EvidenceActivity",
    "EvidenceEventDecision",
    "EvidenceFailure",
    "NudgeUrgency",
    "ProfileAction",
    "ProfileDecision",
    "WorkAssessment",
]
