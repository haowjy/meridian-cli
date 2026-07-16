"""Pi completion collaborators and their drain construction wrapper."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from meridian.lib.core.types import SpawnId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
from meridian.lib.streaming.completion_contracts import (
    AssessmentTrigger,
    CleanupReport,
    DiagnosticBlocker,
    EvidenceActivity,
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
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from meridian.lib.streaming.pi_work_ledger import (
    PiPrivateWorkLedger,
    PiPrivateWorkSnapshot,
)

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.streaming.spawn_session import DrainOutcome

_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
logger = logging.getLogger(__name__)

TerminatePiChildren = Callable[[PiPrivateWorkLedger, str], Awaitable[None]]
PiPersistedDescendantAuthority = Literal["reconciled_tree", "confirmed_child"]
_PI_DESCENDANT_POLL_INTERVAL_SECONDS = 0.25


class PiCompletionEvidence:
    """Combine reconciled descendants with Pi-owned completion blockers."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        tracker: PiSubspawnTracker,
        quiescence_tracker: PiQuiescenceTracker,
        notification_timeout_seconds: float | None,
        clock: Callable[[], float],
        persisted_descendant_authority: PiPersistedDescendantAuthority = "reconciled_tree",
        allocation_barrier: bool = True,
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.tracker = tracker
        self.quiescence_tracker = quiescence_tracker
        self.notification_timeout_seconds = notification_timeout_seconds
        self.persisted_descendant_authority = persisted_descendant_authority
        self.allocation_barrier = allocation_barrier
        self._clock = clock
        self._descendant_evidence = ReconciledDescendantEvidence(
            runtime_root=runtime_root,
            root_spawn_id=spawn_id,
        )
        self._profile: PiCompletionProfile | None = None
        self._generation = 0
        self._last_signature: object = None
        self._last_shadow_signature: object = None
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
        self, event: HarnessEvent, transition: str | None
    ) -> EvidenceEventDecision:
        duplicate = self.tracker.observe(
            event,
            now_monotonic=self._clock(),
            notification_timeout_seconds=self.notification_timeout_seconds,
        )
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

    def note_event_persisted(self, event: HarnessEvent) -> EvidenceEventDecision:
        profile = self._profile
        activity = profile.note_persisted_activity(event) if profile is not None else None
        lifecycle_error = self.tracker.lifecycle_tracking_invalidated_error
        failure = (
            EvidenceFailure(code="pi_lifecycle_tracking_invalidated", detail=lifecycle_error)
            if lifecycle_error is not None
            else None
        )
        if activity is None and self.tracker.notification_failure_error is not None:
            activity = EvidenceActivity(code="pi_notification_failure")
        return EvidenceEventDecision(activity=activity, failure=failure)

    async def assess(self, trigger: AssessmentTrigger) -> WorkAssessment:
        profile = self._profile
        if trigger == "aux_wake" or (
            trigger == "timeout" and profile is not None and profile.micro_drain_active
        ):
            await self.quiescence_tracker.refresh_disk_state()
        if trigger == "aux_wake" and profile is not None:
            profile.after_disk_change()
        reconciled_descendants = self._descendant_evidence.assess()
        self._emit_descendant_evidence_shadow(reconciled_descendants)
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
        return self._assessment(self._descendant_evidence.assess()).disposition == "ready"

    def has_pending_children(self) -> bool:
        descendant_assessment = self._persisted_descendant_assessment(
            self._descendant_evidence.assess()
        )
        private_work = self.quiescence_tracker.private_work_snapshot()
        return (
            descendant_assessment.disposition != "ready"
            or bool(private_work.rowless_subspawn_ids)
            or bool(private_work.allocation_uncertainty_ids)
        )

    def pending_child_count(self) -> int:
        descendant_assessment = self._persisted_descendant_assessment(
            self._descendant_evidence.assess()
        )
        persisted_count = (
            len(descendant_assessment.blockers)
            if descendant_assessment.disposition == "blocked"
            else int(descendant_assessment.disposition == "unknown")
        )
        private_work = self.quiescence_tracker.private_work_snapshot()
        return (
            persisted_count
            + len(private_work.rowless_subspawn_ids)
            + len(private_work.allocation_uncertainty_ids)
        )

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
        if self.persisted_descendant_authority == "reconciled_tree":
            return reconciled_descendants
        blockers = tuple(
            DiagnosticBlocker(source="persisted_descendant", code="pi_direct_child")
            for _ in range(self.quiescence_tracker.pending_child_spawn_count())
        )
        return WorkAssessment(
            disposition="blocked" if blockers else "ready",
            blockers=blockers,
            generation=0,
        )

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
                identity=blocker.identity,
            )
            for blocker in private_work.blockers
            if blocker.kind != "allocation"
            or (
                self.allocation_barrier
                and self.persisted_descendant_authority == "reconciled_tree"
            )
        )
        return tuple(blockers)

    def _emit_descendant_evidence_shadow(self, shadow: WorkAssessment) -> None:
        profile = self._profile
        if profile is None:
            return
        watcher_ids = set(self.quiescence_tracker.pending_confirmed_child_ids())
        tree_ids = {
            blocker.identity for blocker in shadow.blockers if blocker.identity is not None
        }
        records: list[tuple[str, tuple[str, ...]]] = []
        if shadow.disposition == "unknown":
            records.append(("store-error", ()))
        else:
            unresolved_ids = self.quiescence_tracker.unresolved_child_ids()
            if unresolved_ids:
                records.append(("allocation-uncertainty", unresolved_ids))
            overlap = watcher_ids & tree_ids
            if overlap:
                records.append(("both", tuple(sorted(overlap))))
            tree_only = tree_ids - watcher_ids
            if tree_only:
                records.append(("tree-only", tuple(sorted(tree_only))))
            watcher_only = watcher_ids - tree_ids
            if watcher_only:
                records.append(("watcher-only", tuple(sorted(watcher_only))))

        signature = (
            tuple(records),
            len(watcher_ids),
            len(tree_ids),
            shadow.failure,
        )
        if signature == self._last_shadow_signature:
            return
        self._last_shadow_signature = signature

        for category, spawn_ids in records:
            payload: dict[str, object] = {
                "category": category,
                "spawn_ids": spawn_ids,
                "watcher_active_count": len(watcher_ids),
                "tree_active_count": len(tree_ids),
            }
            if shadow.failure is not None:
                payload["error_code"] = shadow.failure.code
                payload["error_detail"] = shadow.failure.detail
            try:
                profile.emit("descendant_evidence_shadow", **payload)
            except Exception:
                logger.warning("Failed to emit Pi descendant shadow phase", exc_info=True)

    def _next_generation(self, signature: object) -> int:
        if signature != self._last_signature:
            self._generation += 1
            self._last_signature = signature
        return self._generation


class PiCompletionCleanup:
    """Terminate Pi-owned cleanup handles through the plan's canonical callback."""

    def __init__(
        self,
        *,
        ledger: PiPrivateWorkLedger,
        quiescence_tracker: PiQuiescenceTracker,
        terminate_children: TerminatePiChildren | None,
        emit_phase: Callable[..., None],
    ) -> None:
        self._ledger = ledger
        self.quiescence_tracker = quiescence_tracker
        self.terminate_children = terminate_children
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
            callback = self.terminate_children
            if callback is None:
                raise RuntimeError("Pi child process-exit cleanup is not configured")
            handles_attempted = self._ledger.tracked_subspawn_ids()
            await callback(self._ledger, reason)
            self.tracked_cleanup_reason = reason
            return CleanupReport(
                attempted_categories=("pi_tracked_children",),
                handles_attempted=handles_attempted,
            )
        if reason != "pi_child_wave_timeout":
            return CleanupReport()
        callback = self.terminate_children
        if callback is None:
            raise RuntimeError("Pi child timeout cleanup is not configured")
        handles_attempted = self._ledger.tracked_subspawn_ids()
        failures: tuple[EvidenceFailure, ...] = ()
        try:
            await callback(self._ledger, reason)
        except Exception as exc:
            self.tracked_cleanup_error = str(exc)
            failures = (EvidenceFailure(code="pi_child_cleanup_failed", detail=str(exc)),)
        finally:
            tracked_count = (
                self._ledger.clear_tracked_subspawns()
                + self.quiescence_tracker.pending_child_spawn_count()
            )
            telemetry = self._child_timeout_telemetry or ChildTimeoutTelemetry(
                active_tracked_count=tracked_count,
                elapsed_seconds=0.0,
                timeout_seconds=0.0,
            )
            payload: dict[str, object] = {
                "active_tracked_count": tracked_count,
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
            handles_attempted=handles_attempted,
            failures=failures,
        )


class PiDrainCoordinator:
    """Thin compatibility wrapper constructing the shared completion coordinator."""

    def __init__(
        self,
        *,
        coordinator: CompletionCoordinator,
        evidence: PiCompletionEvidence,
        profile: PiCompletionProfile,
        cleanup: PiCompletionCleanup,
    ) -> None:
        self._coordinator = coordinator
        self._evidence = evidence
        self._profile = profile
        self._cleanup = cleanup
        self.quiescence_tracker = evidence.quiescence_tracker
        self.tracker = evidence.tracker

    @classmethod
    def for_connection(
        cls,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        receiver: HarnessConnection[ResolvedLaunchSpec],
        session_role: str | None,
        notification_timeout_seconds: float | None,
        child_wave_timeout_seconds: float | None,
        emit_phase: EmitPiPhase,
        terminate_children: TerminatePiChildren | None = None,
        send_done_nudge: SendPiDoneNudge | None = None,
        persisted_descendant_authority: PiPersistedDescendantAuthority = "reconciled_tree",
        allocation_barrier: bool = True,
    ) -> PiDrainCoordinator:
        del receiver
        normalized_role = (session_role or "").strip().lower()
        clock = lambda: time.monotonic()  # noqa: E731 - preserve runtime clock patching
        ledger = PiPrivateWorkLedger()
        tracker = PiSubspawnTracker.empty(ledger)
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
            tracker=tracker,
            quiescence_tracker=quiescence_tracker,
            notification_timeout_seconds=notification_timeout_seconds,
            clock=clock,
            persisted_descendant_authority=persisted_descendant_authority,
            allocation_barrier=allocation_barrier,
        )
        profile = PiCompletionProfile(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            session_role=normalized_role,
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=emit_phase,
            send_done_nudge=send_done_nudge,
            evidence=evidence,
            private_work_ledger=ledger,
            stabilization_seconds=PI_MICRO_DRAIN_TIMEOUT_SECONDS,
            clock=clock,
        )
        cleanup = PiCompletionCleanup(
            ledger=ledger,
            quiescence_tracker=quiescence_tracker,
            terminate_children=terminate_children,
            emit_phase=profile.emit,
        )
        evidence.bind_profile(profile)
        profile.bind_cleanup(cleanup)
        coordinator = CompletionCoordinator(
            evidence=evidence,
            profile=profile,
            cleanup=cleanup,
            clock=clock,
            evaluate_without_candidate=True,
        )
        return cls(
            coordinator=coordinator,
            evidence=evidence,
            profile=profile,
            cleanup=cleanup,
        )

    @property
    def done_nudge_idle_delay_seconds(self) -> float:
        return self._profile.done_nudge_idle_delay_seconds

    @done_nudge_idle_delay_seconds.setter
    def done_nudge_idle_delay_seconds(self, value: float) -> None:
        self._profile.done_nudge_idle_delay_seconds = value

    @property
    def done_nudge_interval_seconds(self) -> float:
        return self._profile.done_nudge_interval_seconds

    @done_nudge_interval_seconds.setter
    def done_nudge_interval_seconds(self, value: float) -> None:
        self._profile.done_nudge_interval_seconds = value

    @property
    def last_successful_terminal(self) -> TerminalEventOutcome | None:
        return self._profile.last_successful_terminal

    @property
    def quiescence_candidate(self) -> TerminalEventOutcome | None:
        return self._coordinator.pending_outcome if self._profile.micro_drain_active else None

    @property
    def pending_outcome(self) -> TerminalEventOutcome | None:
        return self._coordinator.pending_outcome

    @pending_outcome.setter
    def pending_outcome(self, value: TerminalEventOutcome | None) -> None:
        self._coordinator.pending_outcome = value

    @property
    def deadline_monotonic(self) -> float | None:
        return self._coordinator.deadline_monotonic

    @deadline_monotonic.setter
    def deadline_monotonic(self, value: float | None) -> None:
        self._coordinator.deadline_monotonic = value

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

    async def observe_event(self, event: HarnessEvent, transition: str | None) -> bool:
        return await self._coordinator.observe_event(event, transition)

    def note_event_persisted(self, event: HarnessEvent) -> DrainLoopDecision:
        return self._coordinator.note_event_persisted(event)

    async def handle_terminal_event(
        self,
        event: HarnessEvent,
        outcome: TerminalEventOutcome,
        action: DrainAction,
    ) -> DrainTerminalDecision:
        self._profile.observe_terminal_event(event, outcome)
        return await self._coordinator.handle_terminal_event(event, outcome, action)

    async def handle_timeout(
        self,
        terminate_children: TerminatePiChildren | None = None,
    ) -> DrainLoopDecision:
        if not self._profile.quiescence_enabled:
            raise TimeoutError
        prior_callback = self._cleanup.terminate_children
        if terminate_children is not None:
            self._cleanup.terminate_children = terminate_children
        try:
            return await self._coordinator.handle_timeout()
        finally:
            self._cleanup.terminate_children = prior_callback

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
