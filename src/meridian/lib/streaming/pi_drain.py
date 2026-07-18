"""Pi completion collaborators and their drain construction wrapper."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    CompletionCleanupRequest,
    DiagnosticBlocker,
    EvidenceEventDecision,
    EvidenceFailure,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.descendant_evidence import ReconciledDescendantEvidence
from meridian.lib.streaming.drain_coordinator import (
    DrainExitDecision,
    DrainLoopDecision,
    DrainTerminalDecision,
)
from meridian.lib.streaming.drain_policy import (
    DrainAction,
    DrainPolicy,
)
from meridian.lib.streaming.pi_completion_profile import (
    PI_MICRO_DRAIN_TIMEOUT_SECONDS as PI_MICRO_DRAIN_TIMEOUT_SECONDS,
)
from meridian.lib.streaming.pi_completion_profile import (
    ChildTimeoutTelemetry,
    EmitPiPhase,
    PiCompletionProfile,
    PiOutstandingWork,
    SendPiDoneNudge,
)
from meridian.lib.streaming.pi_lifecycle_tracker import PiLifecycleTracker
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_work_ledger import (
    PiPrivateWorkLedger,
    PiPrivateWorkSnapshot,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, RawHarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.streaming.spawn_session import DrainOutcome

_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
logger = logging.getLogger(__name__)

CancelPiDescendants = Callable[[str], Awaitable[None]]
_PI_DESCENDANT_POLL_INTERVAL_SECONDS = 0.25


class PiCompletionEvidence:
    """Combine reconciled descendants with Pi-owned completion blockers."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        lifecycle_tracker: PiLifecycleTracker,
        quiescence_tracker: PiQuiescenceTracker,
        clock: Callable[[], float],
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.lifecycle_tracker = lifecycle_tracker
        self.quiescence_tracker = quiescence_tracker
        self._clock = clock
        self._descendant_evidence = ReconciledDescendantEvidence(
            runtime_root=runtime_root,
            root_spawn_id=spawn_id,
        )
        self._profile: PiCompletionProfile | None = None
        self._generation = 0
        self._last_signature: object = None
        self._next_descendant_poll_at: float | None = None
        self.session_seen = False
        self.session_phase_emitted = False

    def bind_profile(self, profile: PiCompletionProfile) -> None:
        self._profile = profile

    async def start(self) -> None:
        await self.quiescence_tracker.start()

    async def stop(self) -> None:
        await self.quiescence_tracker.stop()
        self._next_descendant_poll_at = None

    async def observe_event(
        self, event: RawHarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        duplicate = self.lifecycle_tracker.observe(event)
        if duplicate:
            return EvidenceEventDecision(duplicate_canonical_event=True)
        if event.event_type == "session":
            self.session_seen = True
        if event.event_type == _PI_PHASE_EVENT_TYPE:
            phase_value = event.payload.get("phase")
            if phase_value in {"session_event_seen", "session_event_absent"}:
                self.session_phase_emitted = True
                if phase_value == "session_event_seen":
                    self.session_seen = True
        if transition == "turn_active":
            self.quiescence_tracker.mark_turn_active()
        elif transition == "idle":
            await self.quiescence_tracker.mark_idle()
        profile = self._profile
        if profile is not None:
            profile.after_observed_event(transition)
        return EvidenceEventDecision()

    def note_event_persisted(self, event: RawHarnessEvent) -> EvidenceEventDecision:
        profile = self._profile
        activity = profile.note_persisted_activity(event) if profile is not None else None
        lifecycle_error = self.lifecycle_tracker.lifecycle_tracking_invalidated_error
        failure = (
            EvidenceFailure(code="pi_lifecycle_tracking_invalidated", detail=lifecycle_error)
            if lifecycle_error is not None
            else None
        )
        return EvidenceEventDecision(activity=activity, failure=failure)

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        profile = self._profile
        if trigger == "aux_wake" or (
            trigger == "timeout" and profile is not None and profile.micro_drain_active
        ):
            await self.quiescence_tracker.refresh_disk_state()
        if trigger == "aux_wake" and profile is not None:
            profile.after_disk_change()
        reconciled_descendants = self._assess_reconciled_descendants()
        if profile is not None and profile.last_successful_terminal is not None:
            self._next_descendant_poll_at = (
                self._clock() + _PI_DESCENDANT_POLL_INTERVAL_SECONDS
            )
        return self._assessment(reconciled_descendants)

    def next_due_at(self) -> float | None:
        return self._next_descendant_poll_at

    async def handle_due(self) -> EvidenceEventDecision:
        self._next_descendant_poll_at = None
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        profile = self._profile
        return bool(profile is not None and profile.quiescence_enabled)

    async def wait_for_change(self) -> None:
        await self.quiescence_tracker.wait_for_disk_change()

    def is_quiescent(self) -> bool:
        return self._assessment(self._assess_reconciled_descendants()).disposition == "ready"

    def has_pending_children(self) -> bool:
        descendant_assessment = self._persisted_descendant_assessment(
            self._assess_reconciled_descendants()
        )
        return descendant_assessment.disposition != "ready"

    def pending_child_count(self) -> int:
        descendant_assessment = self._persisted_descendant_assessment(
            self._assess_reconciled_descendants()
        )
        persisted_count = (
            len(descendant_assessment.blockers)
            if descendant_assessment.disposition == "blocked"
            else int(descendant_assessment.disposition == "unknown")
        )
        return persisted_count

    def classify_outstanding_work(self) -> PiOutstandingWork:
        reconciled_descendants = self._assess_reconciled_descendants()
        private_work = self.quiescence_tracker.private_work_snapshot()
        if reconciled_descendants.disposition == "unknown":
            return PiOutstandingWork(
                spawn_children=True,
                non_spawn_processes=private_work.tracked_bash_bg,
            )

        return PiOutstandingWork(
            spawn_children=bool(reconciled_descendants.blockers),
            non_spawn_processes=private_work.tracked_bash_bg,
        )

    def _assess_reconciled_descendants(self) -> WorkAssessment:
        return self._descendant_evidence.assess()

    def _assessment(self, reconciled_descendants: WorkAssessment) -> WorkAssessment:
        descendants = self._persisted_descendant_assessment(reconciled_descendants)
        private_work = self.quiescence_tracker.private_work_snapshot()
        private_failure = private_work.failure
        if descendants.disposition == "unknown" or private_failure is not None:
            failure = descendants.failure or private_failure
            assert failure is not None
            signature = ("unknown", failure.code, failure.detail)
            return WorkAssessment(
                disposition="unknown",
                blockers=(),
                generation=self._next_generation(signature),
                failure=failure,
            )

        blockers = descendants.blockers + self._pi_owned_blockers(private_work)
        signature = tuple((item.code, item.identity) for item in blockers)
        if blockers:
            return WorkAssessment(
                disposition="blocked",
                blockers=blockers,
                generation=self._next_generation(signature),
            )
        return WorkAssessment(
            disposition="ready",
            blockers=(),
            generation=self._next_generation(signature),
        )

    def _persisted_descendant_assessment(
        self, reconciled_descendants: WorkAssessment
    ) -> WorkAssessment:
        return reconciled_descendants

    def _pi_owned_blockers(
        self,
        private_work: PiPrivateWorkSnapshot,
    ) -> tuple[DiagnosticBlocker, ...]:
        blockers: list[DiagnosticBlocker] = []
        if not self.quiescence_tracker.parent_idle:
            blockers.append(DiagnosticBlocker(source="profile", code="pi_parent_active"))
        blockers.extend(
            DiagnosticBlocker(
                source="profile",
                code=blocker.code,
            )
            for blocker in private_work.blockers
        )
        return tuple(blockers)

    def _next_generation(self, signature: object) -> int:
        if signature != self._last_signature:
            self._generation += 1
            self._last_signature = signature
        return self._generation


class PiCompletionCleanup:
    """Cancel persisted Pi descendants through the plan's injected callback."""

    def __init__(
        self,
        *,
        quiescence_tracker: PiQuiescenceTracker,
        cancel_descendants: CancelPiDescendants | None,
        emit_phase: Callable[..., None],
    ) -> None:
        self.quiescence_tracker = quiescence_tracker
        self.cancel_descendants = cancel_descendants
        self.emit_phase = emit_phase
        self.tracked_cleanup_reason: str | None = None
        self.tracked_cleanup_error: str | None = None
        self._child_timeout_telemetry: ChildTimeoutTelemetry | None = None

    def prepare_child_timeout(self, telemetry: ChildTimeoutTelemetry) -> None:
        if self.tracked_cleanup_reason is None:
            self.tracked_cleanup_reason = "pi_child_wave_timeout"
        self._child_timeout_telemetry = telemetry

    async def cleanup(self, assessment: WorkAssessment, reason: str) -> CleanupReport:
        del assessment
        if reason == "pi_process_exit_with_tracked_children":
            if self.tracked_cleanup_reason is not None:
                return CleanupReport()
            callback = self.cancel_descendants
            if callback is None:
                raise RuntimeError("Pi child process-exit cleanup is not configured")
            await callback(reason)
            self.tracked_cleanup_reason = reason
            return CleanupReport(
                attempted_categories=("pi_tracked_children",),
            )
        if reason != "pi_child_wave_timeout":
            return CleanupReport()
        callback = self.cancel_descendants
        if callback is None:
            raise RuntimeError("Pi child timeout cleanup is not configured")
        failures: tuple[EvidenceFailure, ...] = ()
        try:
            await callback(reason)
        except Exception as exc:
            self.tracked_cleanup_error = str(exc)
            failures = (EvidenceFailure(code="pi_child_cleanup_failed", detail=str(exc)),)
        finally:
            telemetry = self._child_timeout_telemetry or ChildTimeoutTelemetry(
                active_tracked_count=0,
                elapsed_seconds=0.0,
                timeout_seconds=0.0,
            )
            payload: dict[str, object] = {
                "active_tracked_count": telemetry.active_tracked_count,
                "elapsed_seconds": telemetry.elapsed_seconds,
                "timeout_seconds": telemetry.timeout_seconds,
            }
            if self.tracked_cleanup_error is not None:
                payload["cleanup_error"] = self.tracked_cleanup_error
            try:
                self.emit_phase("pi_child_wave_timeout", **payload)
            except Exception:
                logger.warning("Failed to emit Pi child-wave timeout phase", exc_info=True)
        return CleanupReport(
            attempted_categories=("pi_tracked_children",),
            failures=failures,
        )


class PiDrainCoordinator:
    """Construct and adapt Pi collaborators to the drain coordinator protocol."""

    def __init__(
        self,
        *,
        coordinator: CompletionCoordinator,
        evidence: PiCompletionEvidence,
        profile: PiCompletionProfile,
    ) -> None:
        self._coordinator = coordinator
        self._evidence = evidence
        self._profile = profile

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[ResolvedLaunchSpec],
        session_role: str | None,
        child_wave_timeout_seconds: float | None,
        emit_phase: EmitPiPhase,
        cancel_descendants: CancelPiDescendants | None = None,
        send_done_nudge: SendPiDoneNudge | None = None,
    ) -> PiDrainCoordinator:
        del receiver
        normalized_role = (session_role or "").strip().lower()
        clock = lambda: time.monotonic()  # noqa: E731 - preserve runtime clock patching
        ledger = PiPrivateWorkLedger()
        lifecycle_tracker = PiLifecycleTracker.empty()
        quiescence_tracker = PiQuiescenceTracker.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            is_pi_connection=True,
            session_role=normalized_role,
            ledger=ledger,
        )
        evidence = PiCompletionEvidence(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            lifecycle_tracker=lifecycle_tracker,
            quiescence_tracker=quiescence_tracker,
            clock=clock,
        )
        profile = PiCompletionProfile(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            session_role=normalized_role,
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=emit_phase,
            send_done_nudge=send_done_nudge,
            evidence=evidence,
            stabilization_seconds=PI_MICRO_DRAIN_TIMEOUT_SECONDS,
            clock=clock,
        )
        cleanup = PiCompletionCleanup(
            quiescence_tracker=quiescence_tracker,
            cancel_descendants=cancel_descendants,
            emit_phase=profile.emit,
        )
        evidence.bind_profile(profile)
        profile.bind_cleanup(cleanup)
        coordinator = CompletionCoordinator(
            evidence=evidence,
            profile=profile,
            cleanup=cleanup,
            clock=clock,
        )
        return cls(
            coordinator=coordinator,
            evidence=evidence,
            profile=profile,
        )

    async def start(self) -> None:
        await self._coordinator.start()
        self._profile.start()

    async def stop(self) -> None:
        self._profile.stop()
        await self._coordinator.stop()

    def set_policy(self, policy: DrainPolicy) -> None:
        self._profile.set_policy(policy)

    def next_timeout(self) -> float | None:
        return self._coordinator.next_timeout()

    async def observe_event(self, event: RawHarnessEvent, transition: str | None) -> bool:
        return await self._coordinator.observe_event(event, transition)

    def note_event_persisted(self, event: RawHarnessEvent) -> DrainLoopDecision:
        return self._coordinator.note_event_persisted(event)

    async def handle_terminal_event(
        self,
        event: RawHarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        self._profile.observe_terminal_event(event, outcome)
        return await self._coordinator.handle_terminal_event(event, outcome, action)

    async def handle_timeout(self) -> DrainLoopDecision:
        if not self._profile.quiescence_enabled:
            raise TimeoutError
        return await self._coordinator.handle_timeout()

    async def after_event(self) -> DrainLoopDecision:
        return await self._coordinator.after_event()

    def wants_aux_wake(self) -> bool:
        return self._coordinator.wants_aux_wake()

    async def wait_for_aux_wake(self) -> None:
        await self._coordinator.wait_for_aux_wake()

    async def handle_aux_wake(self) -> DrainLoopDecision:
        return await self._coordinator.handle_aux_wake()

    async def reevaluate_after_disk_change(self) -> DrainLoopDecision:
        if not self._profile.quiescence_enabled:
            return DrainLoopDecision()
        return await self._coordinator.handle_aux_wake()

    def handle_close(self, *, intentional_stop: bool) -> TerminalEventOutcome | None:
        return self._coordinator.handle_close(intentional_stop=intentional_stop)

    async def handle_stream_exit(
        self,
        recorded_outcome: TerminalEventOutcome | None,
    ) -> DrainExitDecision:
        return await self._coordinator.handle_stream_exit(recorded_outcome)

    async def execute_post_publication_cleanup(
        self,
        request: CompletionCleanupRequest,
    ) -> None:
        await self._coordinator.execute_post_publication_cleanup(request)

    def after_finalized(
        self,
        *,
        connection_session_id: str | None,
        outcome: DrainOutcome,
    ) -> None:
        self._profile.after_finalized(
            connection_session_id=connection_session_id,
            outcome=outcome,
        )

    def is_quiescent(self) -> bool:
        return self._evidence.is_quiescent()

    def classify_outstanding_work(self) -> PiOutstandingWork:
        return self._profile.classify_outstanding_work()


__all__ = ["PiDrainCoordinator"]
