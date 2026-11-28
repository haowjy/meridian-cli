from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from meridian.lib.core.delivery import DeliveryKind
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_policy import SingleTurnDrainPolicy
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
        RawHarnessEvent,
    )

EmitEvent = Callable[[SpawnId, "RawHarnessEvent"], None]


class SerializedDeliver(Protocol):
    """Manager-owned serialized delivery capability."""

    def __call__(self, spawn_id: SpawnId, text: str, *, source: str) -> Awaitable[DeliveryKind]: ...


class DescendantCancellationService(Protocol):
    """Application-service capability needed by tracked-child cleanup."""

    def cancel_descendants(self, root_id: SpawnId | str) -> Awaitable[set[str]]: ...


BuildSpawnApplicationService = Callable[[Path, Path], DescendantCancellationService]


@dataclass(frozen=True)
class DrainPlanContext:
    """What a connection's own drain plan may use (`HarnessConnection.drain_plan`)."""

    launch: LaunchPaths
    config: ConnectionConfig
    emit_event: EmitEvent
    deliver: SerializedDeliver
    cancel_descendants: Callable[[SpawnId], Awaitable[set[str]]]


def build_drain_plan(
    *,
    project_root: Path,
    launch: LaunchPaths,
    receiver: HarnessConnection[Any],
    config: ConnectionConfig,
    emit_event: EmitEvent,
    deliver: SerializedDeliver,
    build_spawn_application_service: BuildSpawnApplicationService,
) -> DrainPlan:

    spawn_id = launch.spawn_id

    async def _cancel_descendants(root_id: SpawnId) -> set[str]:
        service = build_spawn_application_service(project_root, launch.runtime_root)
        return await service.cancel_descendants(root_id)

    resident_backend = receiver.resident_backend
    native = receiver.native_task_source()
    if native is not None or resident_backend is not None:
        coordinator = ResidentDrainCoordinator.build(
            launch=launch,
            send_nudge=partial(deliver, spawn_id, source="completion_nudge"),
            completion_signals_available=receiver.completion_signals_available,
            backend=resident_backend,
            backend_dead=receiver.backend_dead,
            native=native,
            deadline_seconds=config.resident_deadline_seconds,
            poll_seconds=config.resident_poll_seconds,
            rearm_budget=config.resident_rearm_budget,
            cancel_descendants=_cancel_descendants,
        )
        return DrainPlan(
            coordinator=coordinator,
            policy=SingleTurnDrainPolicy(),
            raw_terminal_frames_authoritative=False,
            aux_wake=coordinator,
            handle_aux_wake=coordinator.handle_aux_wake,
            accepted_signals=coordinator.accepted_signals,
        )

    own_plan = receiver.drain_plan(
        DrainPlanContext(
            launch=launch,
            config=config,
            emit_event=emit_event,
            deliver=deliver,
            cancel_descendants=_cancel_descendants,
        )
    )
    if own_plan is not None:
        return own_plan

    # Plain streaming harnesses use SpawnDrainLoop's single-turn baseline and
    # consume no completion signals.
    return DrainPlan()


__all__ = ["DrainPlanContext", "EmitEvent", "build_drain_plan"]
