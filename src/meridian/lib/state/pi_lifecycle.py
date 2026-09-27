"""Bounded Pi lifecycle diagnostics, independent of transcript persistence."""

from pathlib import Path

from pydantic import BaseModel, ValidationError

from meridian.lib.core.types import SpawnId
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import pi_lifecycle_path
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact


class PiLifecycle(BaseModel):
    phase: str | None = None
    attempt: str | int | None = None
    cleanup_status: str | None = None
    cleanup_phase: str | None = None
    reason: str | None = None
    error: str | None = None


def read(runtime_root: Path, spawn_id: SpawnId | str) -> PiLifecycle:
    try:
        return PiLifecycle.model_validate_json(
            pi_lifecycle_path(runtime_root, spawn_id).read_bytes()
        )
    except (OSError, ValidationError):
        return PiLifecycle()


def record(runtime_root: Path, spawn_id: SpawnId | str, event: PiLifecycle) -> None:
    def update() -> None:
        prior = read(runtime_root, spawn_id)
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
        value = prior.model_copy(update=updated)
        atomic_write_text(
            pi_lifecycle_path(runtime_root, spawn_id),
            value.model_dump_json(exclude_none=True) + "\n",
        )

    mutate_published_spawn_artifact(runtime_root, SpawnId(str(spawn_id)), update)
