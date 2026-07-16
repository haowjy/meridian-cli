"""Runtime-root bootstrap services for Meridian state."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.ops.runtime import (
    RuntimeAuthoritySnapshot,
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
)
from meridian.lib.state.paths import RuntimePaths


def resolve_runtime_root_for_read(
    project_root: Path,
    *,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> Path | None:
    """Resolve runtime root for read paths without creating a project UUID."""

    resolved_authority = authority or resolve_runtime_authority_for_read(project_root)
    return resolved_authority.runtime_root


def ensure_runtime_root(
    project_root: Path,
    *,
    authority: RuntimeAuthoritySnapshot | None = None,
) -> Path:
    """Ensure and return the write runtime root, creating project UUID if needed."""

    resolved_authority = authority or resolve_runtime_authority_for_write(project_root)
    if resolved_authority.runtime_root is None:
        raise ValueError("Runtime write authority did not resolve a runtime root.")
    return resolved_authority.runtime_root


def ensure_runtime_dirs(runtime_root: Path) -> None:
    """Create runtime directories required by spawn/session/telemetry writers."""

    runtime_paths = RuntimePaths.from_root_dir(runtime_root)
    for dir_path in (
        runtime_paths.root_dir,
        runtime_paths.spawns_dir,
        runtime_paths.sessions_dir,
        runtime_paths.chats_dir,
        runtime_root / "telemetry",
    ):
        dir_path.mkdir(parents=True, exist_ok=True)

    from meridian.lib.state.spawn_store import gc_abandoned_stages

    gc_abandoned_stages(runtime_root)
