"""Composition root for streaming drain plans."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.pi_lifecycle_events import build_pi_phase_event
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.streaming import pi_process_cleanup
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_policy import (
    PiRpcQuiescenceDrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.pi_drain_teardown import EmitEvent, PiDrainSessionTeardown
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from meridian.lib.streaming.types import InjectResult

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
    )

logger = logging.getLogger(__name__)


class SerializedInject(Protocol):
    """Manager-owned serialized injection capability."""

    def __call__(
        self,
        spawn_id: SpawnId,
        message: str,
        source: str = "control_socket",
        on_result: Callable[[InjectResult], None] | None = None,
    ) -> Awaitable[InjectResult]: ...


class DescendantCancellationService(Protocol):
    """Application-service capability needed by Pi tracked-child cleanup."""

    def cancel_descendants(self, root_id: SpawnId | str) -> Awaitable[set[str]]: ...


BuildSpawnApplicationService = Callable[[Path, Path], DescendantCancellationService]


def build_drain_plan(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: SpawnId,
    receiver: HarnessConnection[Any],
    config: ConnectionConfig,
    emit_event: EmitEvent,
    inject: SerializedInject,
    build_spawn_application_service: BuildSpawnApplicationService,
) -> DrainPlan:
    """Build the complete drain-loop configuration for one active spawn."""

    def _emit_pi_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        emit_event(
            spawn_id,
            build_pi_phase_event(
                spawn_id,
                receiver,
                phase,
                session_role=session_role,
                **payload,
            ),
        )

    async def _terminate_tracked_pi_children(
        ledger: PiPrivateWorkLedger,
        reason: str,
    ) -> None:
        reaped_descendant_ids: set[str] = set()
        try:
            service = build_spawn_application_service(project_root, runtime_root)
            reaped_descendant_ids = await service.cancel_descendants(spawn_id)
        except Exception:
            logger.exception(
                "Failed to cancel Pi descendant spawns before tracked child cleanup.",
                extra={"spawn_id": str(spawn_id), "reason": reason},
            )
            # Intentional: if canonical descendant cancellation fails, fall
            # back to the full ledger cleanup-handle set so Pi-internal cleanup is not
            # skipped because we could not prove which ids were reaped.
            reaped_descendant_ids = set()
        await pi_process_cleanup.terminate_pi_tracked_subspawns(
            spawn_id,
            ledger,
            reason=reason,
            exclude_subspawn_ids=reaped_descendant_ids,
        )

    async def _send_pi_done_nudge(message: str) -> None:
        await inject(spawn_id, message, source="pi_done_nudge")

    async def _cancel_descendants(root_id: SpawnId) -> set[str]:
        service = build_spawn_application_service(project_root, runtime_root)
        return await service.cancel_descendants(root_id)

    resident_backend = receiver.resident_backend
    if resident_backend is not None:
        coordinator = ResidentDrainCoordinator.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=receiver,
            resident_backend=resident_backend,
            deadline_seconds=config.resident_deadline_seconds,
            poll_seconds=config.resident_poll_seconds,
            cancel_descendants=_cancel_descendants,
        )
        return DrainPlan(
            coordinator=coordinator,
            policy=SingleTurnDrainPolicy(),
            raw_terminal_frames_authoritative=False,
        )

    if receiver.harness_id is HarnessId.PI:
        coordinator = PiDrainCoordinator.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=cast("HarnessConnection[ResolvedLaunchSpec]", receiver),
            session_role=config.pi_session_role,
            notification_timeout_seconds=config.pi_notification_timeout_seconds,
            child_wave_timeout_seconds=config.pi_child_wave_timeout_seconds,
            emit_phase=_emit_pi_phase,
            terminate_children=_terminate_tracked_pi_children,
            send_done_nudge=_send_pi_done_nudge,
        )
        return DrainPlan(
            coordinator=coordinator,
            policy=PiRpcQuiescenceDrainPolicy(
                quiescence_check=coordinator.is_quiescent,
            ),
            raw_terminal_frames_authoritative=False,
            on_policy_selected=coordinator.set_policy,
            aux_wake=coordinator,
            handle_aux_wake=coordinator.handle_aux_wake,
            finalizer=coordinator,
            teardown=PiDrainSessionTeardown(
                spawn_id=spawn_id,
                emit_event=emit_event,
            ),
        )

    # Plain streaming harnesses use SpawnDrainLoop's single-turn baseline.
    return DrainPlan()


__all__ = ["build_drain_plan"]
