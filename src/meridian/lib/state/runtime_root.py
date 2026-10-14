"""Runtime state root derivation from project identity (read-only)."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.project_identity import (
    AdHoc,
    ensure_project_identity,
    resolve_project_identity,
)
from meridian.lib.state.user_paths import get_project_home


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
        identity = ensure_project_identity(project_root)
        if identity.id is None:
            raise ValueError("Runtime write identity did not resolve a project ID.")
        return get_project_home(identity.id)

    identity = resolve_project_identity(project_root)
    if isinstance(identity, AdHoc) and not identity.materialized:
        return None
    if identity.id is None:
        return None
    return get_project_home(identity.id)


__all__ = ["derive_runtime_root_from_project", "root_has_runtime_state"]
