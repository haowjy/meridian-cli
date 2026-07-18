"""Runtime state root derivation from project identity (read-only)."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.user_paths import (
    get_or_create_project_id,
    get_project_home,
)
from meridian.lib.state.user_paths import (
    get_project_id_with_legacy_fallback as read_project_id,
)


def root_has_runtime_state(runtime_root: Path) -> bool:
    return any(
        path.exists()
        for path in (
            runtime_root / "spawns.jsonl",
            runtime_root / "sessions.jsonl",
            runtime_root / "spawns",
            runtime_root / "sessions",
            runtime_root / "telemetry",
        )
    )


def derive_runtime_root_from_project(
    project_root: Path,
    *,
    for_write: bool = False,
) -> Path | None:
    """Derive runtime state root from project identity without reading ``_MERIDIAN_RUNTIME_DIR``."""

    if for_write:
        return get_project_home(get_or_create_project_id(project_root))

    project_id = read_project_id(project_root)
    if project_id is not None:
        return get_project_home(project_id)
    return None


__all__ = ["derive_runtime_root_from_project", "root_has_runtime_state"]
