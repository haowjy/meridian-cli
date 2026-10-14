"""Bounded Pi lifecycle diagnostics, independent of transcript persistence."""

from pathlib import Path

from pydantic import BaseModel

from meridian.lib.core.types import SpawnId
from meridian.lib.state.json_doc import mutate_model, read_model
from meridian.lib.state.paths import pi_lifecycle_path
from meridian.lib.state.spawn_aggregate import (
    PublishedArtifactUnavailable,
    published_artifact_lock,
)


class PiLifecycle(BaseModel):
    phase: str | None = None
    attempt: str | int | None = None
    cleanup_status: str | None = None
    cleanup_phase: str | None = None
    reason: str | None = None
    error: str | None = None


def read(runtime_root: Path, spawn_id: SpawnId | str) -> PiLifecycle:
    return read_model(pi_lifecycle_path(runtime_root, spawn_id), PiLifecycle) or PiLifecycle()


def record(runtime_root: Path, spawn_id: SpawnId | str, event: PiLifecycle) -> None:
    resolved_spawn_id = SpawnId(str(spawn_id))
    path = pi_lifecycle_path(runtime_root, resolved_spawn_id)

    def update(prior: PiLifecycle | None) -> tuple[PiLifecycle, None]:
        prior = prior or PiLifecycle()
        status_rank = {"running": 0, "completed": 1, "escalated": 2, "failed": 3}
        updated = event.model_dump(exclude_none=True)
        if status_rank.get(event.cleanup_status or "", -1) < status_rank.get(
            prior.cleanup_status or "", -1
        ):
            updated.pop("cleanup_status", None)
        if event.phase == "cleanup_escalated":
            updated["cleanup_status"] = "escalated"
        elif event.phase == "cleanup_failed":
            updated["cleanup_status"] = "failed"
        if event.phase and event.phase.startswith("cleanup_"):
            updated["cleanup_phase"] = event.phase
        return prior.model_copy(update=updated), None

    try:
        mutate_model(
            path,
            PiLifecycle,
            update,
            lock=published_artifact_lock(runtime_root, resolved_spawn_id),
            exclude_none=True,
        )
    except PublishedArtifactUnavailable:
        return
