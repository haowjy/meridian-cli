"""Composition root for streaming drain plans."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.pi_lifecycle_events import build_pi_phase_event
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_policy import (
    PiRpcQuiescenceDrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.pi_drain_teardown import EmitEvent, PiDrainSessionTeardown
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from meridian.lib.streaming.types import InjectResult

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
        RawHarnessEvent,
    )

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
RegisterEventHook = Callable[[SpawnId, Callable[["RawHarnessEvent"], None]], None]


def record_pi_lifecycle_event(
    *, runtime_root: Path, spawn_id: SpawnId, event: RawHarnessEvent
) -> None:
    """Atomically retain bounded Pi lifecycle diagnostics outside runner history."""
    if event.event_type != "meridian.pi.lifecycle.phase":
        return
    phase_value = event.payload.get("phase")
    if not isinstance(phase_value, str) or not phase_value.strip():
        return
    phase = phase_value.strip()
    path = runtime_root / "spawns" / str(spawn_id) / "pi-lifecycle.json"
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prior = {}
    if not isinstance(prior, dict):
        prior = {}
    updated = dict(cast("dict[str, object]", prior))
    attempt = event.payload.get("attempt")
    if isinstance(attempt, (str, int)):
        updated["attempt"] = attempt
    status_value = event.payload.get("cleanup_status")
    status = status_value.strip() if isinstance(status_value, str) else ""
    status_rank = {"running": 0, "completed": 1, "escalated": 2, "failed": 3}
    previous_status = updated.get("cleanup_status")
    if status and status_rank.get(status, -1) >= status_rank.get(
        previous_status if isinstance(previous_status, str) else "", -1
    ):
        updated["cleanup_status"] = status
    for key in ("reason", "error"):
        value = event.payload.get(key)
        if isinstance(value, str) and value.strip():
            updated[key] = value.strip()
    updated["phase"] = phase
    if phase == "cleanup_escalated":
        updated["cleanup_status"] = "escalated"
        updated["cleanup_phase"] = phase
    elif phase == "cleanup_failed":
        updated["cleanup_status"] = "failed"
        updated["cleanup_phase"] = phase
    elif phase.startswith("cleanup_"):
        updated["cleanup_phase"] = phase
    atomic_write_text(path, json.dumps(updated, sort_keys=True) + "\n")


def build_drain_plan(
    *,
    project_root: Path,
    runtime_root: Path,
    spawn_id: SpawnId,
    receiver: HarnessConnection[Any],
    config: ConnectionConfig,
    emit_event: EmitEvent,
    register_event_hook: RegisterEventHook,
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

    async def _cancel_pi_descendants(
        reason: str,
    ) -> None:
        del reason
        service = build_spawn_application_service(project_root, runtime_root)
        await service.cancel_descendants(spawn_id)

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
            rearm_budget=config.resident_rearm_budget,
            cancel_descendants=_cancel_descendants,
        )
        return DrainPlan(
            coordinator=coordinator,
            policy=SingleTurnDrainPolicy(),
            raw_terminal_frames_authoritative=False,
            aux_wake=coordinator,
            handle_aux_wake=coordinator.handle_aux_wake,
        )

    if receiver.harness_id is HarnessId.PI:
        register_event_hook(
            spawn_id,
            lambda event: record_pi_lifecycle_event(
                runtime_root=runtime_root, spawn_id=spawn_id, event=event
            ),
        )
        coordinator = PiDrainCoordinator.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=cast("HarnessConnection[ResolvedLaunchSpec]", receiver),
            session_role=config.pi_session_role,
            child_wave_timeout_seconds=config.pi_child_wave_timeout_seconds,
            emit_phase=_emit_pi_phase,
            cancel_descendants=_cancel_pi_descendants,
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


__all__ = ["build_drain_plan", "record_pi_lifecycle_event"]
