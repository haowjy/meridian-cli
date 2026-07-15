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
    DiagnosticBlocker,
    EvidenceActivity,
    EvidenceEventDecision,
    EvidenceFailure,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
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

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection, HarnessEvent
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.streaming.spawn_session import DrainOutcome

_PI_PHASE_EVENT_TYPE = pi_lifecycle.PI_PHASE_EVENT_TYPE
logger = logging.getLogger(__name__)

TerminatePiChildren = Callable[[PiSubspawnTracker, str], Awaitable[None]]


class PiCompletionEvidence:
    """Delegate Pi readiness to the current tracker and disk-backed authority."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        tracker: PiSubspawnTracker,
        quiescence_tracker: PiQuiescenceTracker,
        notification_timeout_seconds: float | None,
        clock: Callable[[], float],
    ) -> None:
        self.runtime_root = runtime_root
        self.spawn_id = spawn_id
        self.tracker = tracker
        self.quiescence_tracker = quiescence_tracker
        self.notification_timeout_seconds = notification_timeout_seconds
        self._clock = clock
        self._profile: PiCompletionProfile | None = None
        self._generation = 0
        self._last_signature: object = None
        self.session_seen = False
        self.session_phase_emitted = False

    def bind_profile(self, profile: PiCompletionProfile) -> None:
        self._profile = profile

    async def start(self) -> None:
        await self.quiescence_tracker.start()

    async def stop(self) -> None:
        await self.quiescence_tracker.stop()

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
        blockers = self._blockers()
        signature = tuple((item.code, item.identity) for item in blockers)
        return (
            WorkAssessment(
                disposition="blocked",
                blockers=blockers,
                generation=self._next_generation(signature),
            )
            if blockers
            else WorkAssessment(
                disposition="ready",
                blockers=(),
                generation=self._next_generation(signature),
            )
        )

    def next_due_at(self) -> float | None:
        return None

    async def handle_due(self) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        profile = self._profile
        return bool(profile is not None and profile.quiescence_enabled)

    async def wait_for_change(self) -> None:
        await self.quiescence_tracker.wait_for_disk_change()

    def is_quiescent(self) -> bool:
        return (
            self.quiescence_tracker.is_quiescent()
            and not self.tracker.has_pending()
            and not self.tracker.has_pending_notifications()
        )

    def has_pending_children(self) -> bool:
        return self.tracker.has_pending() or self.quiescence_tracker.has_pending_child_spawns()

    def pending_child_count(self) -> int:
        return (
            self.tracker.active_tracked_count()
            + self.quiescence_tracker.pending_child_spawn_count()
        )

    def _blockers(self) -> tuple[DiagnosticBlocker, ...]:
        blockers: list[DiagnosticBlocker] = []
        if not self.quiescence_tracker.parent_idle:
            blockers.append(DiagnosticBlocker(source="profile", code="pi_parent_active"))
        blockers.extend(
            DiagnosticBlocker(
                source="persisted_descendant",
                code="pi_tracked_child",
                identity=child_id,
            )
            for child_id in sorted(self.tracker.active_ids)
        )
        disk_child_count = self.quiescence_tracker.pending_child_spawn_count()
        blockers.extend(
            DiagnosticBlocker(source="persisted_descendant", code="pi_direct_child")
            for _ in range(disk_child_count)
        )
        if self.quiescence_tracker.has_tracked_bash_bg():
            blockers.append(DiagnosticBlocker(source="profile", code="pi_tracked_bash_bg"))
        pending_notifications = self.tracker.pending_notifications or {}
        blockers.extend(
            DiagnosticBlocker(
                source="profile",
                code="pi_pending_notification",
                identity=notification_id,
            )
            for notification_id in sorted(pending_notifications)
        )
        if not blockers and not self.quiescence_tracker.is_quiescent():
            blockers.append(DiagnosticBlocker(source="profile", code="pi_disk_notification"))
        return tuple(blockers)

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
        tracker: PiSubspawnTracker,
        quiescence_tracker: PiQuiescenceTracker,
        terminate_children: TerminatePiChildren | None,
        emit_phase: Callable[..., None],
    ) -> None:
        self.tracker = tracker
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
            if not self.tracker.has_pending() or self.tracked_cleanup_reason is not None:
                return CleanupReport()
            callback = self.terminate_children
            if callback is None:
                raise RuntimeError("Pi child process-exit cleanup is not configured")
            handles_attempted = tuple(sorted(self.tracker.active_ids))
            await callback(self.tracker, reason)
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
        handles_attempted = tuple(sorted(self.tracker.active_ids))
        failures: tuple[EvidenceFailure, ...] = ()
        try:
            await callback(self.tracker, reason)
        except Exception as exc:
            self.tracked_cleanup_error = str(exc)
            failures = (EvidenceFailure(code="pi_child_cleanup_failed", detail=str(exc)),)
        finally:
            tracked_count = (
                self.tracker.clear_tracked_children_after_wave_timeout()
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
    ) -> PiDrainCoordinator:
        del receiver
        normalized_role = (session_role or "").strip().lower()
        clock = lambda: time.monotonic()  # noqa: E731 - preserve runtime clock patching
        tracker = PiSubspawnTracker.empty()
        quiescence_tracker = PiQuiescenceTracker.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            is_pi_connection=True,
            session_role=normalized_role,
        )
        evidence = PiCompletionEvidence(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            tracker=tracker,
            quiescence_tracker=quiescence_tracker,
            notification_timeout_seconds=notification_timeout_seconds,
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
            tracker=tracker,
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
