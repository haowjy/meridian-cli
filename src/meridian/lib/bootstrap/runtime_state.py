"""Runtime-root bootstrap services for Meridian state."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.paths import (
    RuntimePaths,
    resolve_project_paths,
    resolve_project_runtime_root_for_write,
    resolve_project_runtime_root_or_none,
)


def _root_has_runtime_state(runtime_root: Path) -> bool:
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


def resolve_runtime_root_for_read(project_root: Path) -> Path | None:
    """Resolve runtime root for read paths without creating a project UUID."""

    project_state_dir = resolve_project_paths(project_root).root_dir
    runtime_root = resolve_project_runtime_root_or_none(project_root)
    if runtime_root is None:
        return project_state_dir if _root_has_runtime_state(project_state_dir) else None

    if runtime_root == project_state_dir:
        return runtime_root

    if not _root_has_runtime_state(runtime_root) and _root_has_runtime_state(project_state_dir):
        return project_state_dir

    return runtime_root


def ensure_runtime_root(project_root: Path) -> Path:
    """Ensure and return the write runtime root, creating project UUID if needed."""

    return resolve_project_runtime_root_for_write(project_root)


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
