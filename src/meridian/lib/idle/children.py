"""Read-only active-child-spawn guard support."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state import spawn_store
from meridian.lib.state.runtime_root import derive_runtime_root_from_project
from meridian.lib.state.spawn_tree import iter_run_parent_descendants


@dataclass(frozen=True)
class SpawnRow:
    """Minimal spawn-store projection needed by the child guard."""

    id: str
    parent_id: str | None
    status: str
    history_id: UUID | None = None
    parent_history_id: UUID | None = None


SpawnStoreReader = Callable[[], Iterable[SpawnRow]]


def active_child_count(parent_spawn_id: str | None, rows: Iterable[SpawnRow]) -> int:
    """Count active run rows in transitive child spawns, tolerating cycles."""

    if not parent_spawn_id:
        return 0
    return sum(
        is_active_spawn_status(row.status)
        for row in iter_run_parent_descendants(parent_spawn_id, rows)
    )


def spawn_store_reader(env: Mapping[str, str]) -> SpawnStoreReader:
    """Build a read-only reader from inherited runtime/project handles."""

    runtime_value = env.get("_MERIDIAN_RUNTIME_DIR", "").strip()
    runtime_root: Path | None = Path(runtime_value).expanduser() if runtime_value else None
    if runtime_root is None:
        project_value = env.get("MERIDIAN_PROJECT_DIR", "").strip()
        if project_value:
            runtime_root = derive_runtime_root_from_project(Path(project_value).expanduser())

    def read() -> Iterable[SpawnRow]:
        if runtime_root is None:
            return ()
        return (
            SpawnRow(
                id=row.id,
                parent_id=row.parent_id,
                status=str(row.status),
                history_id=row.history_id,
                parent_history_id=row.parent_history_id,
            )
            for row in spawn_store.list_spawns(runtime_root).records
        )

    return read


__all__ = ["SpawnRow", "SpawnStoreReader", "active_child_count", "spawn_store_reader"]
