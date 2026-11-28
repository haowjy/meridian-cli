from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.state.project_identity import (
    ADHOC_PROJECT_ID_PREFIX,
    ensure_project_identity,
    resolve_project_identity,
    state_id,
)
from meridian.lib.state.user_paths import get_project_home, get_user_home


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
    if for_write:
        identity = ensure_project_identity(project_root)
        project_id = state_id(identity)
        if project_id is None:
            raise ValueError("Runtime write identity did not resolve a project ID.")
        return get_project_home(project_id)

    identity = resolve_project_identity(project_root)
    project_id = state_id(identity)
    if project_id is None:
        return None
    return get_project_home(project_id)


def runtime_override_root(
    project_root: Path | None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    override = env.get("_MERIDIAN_RUNTIME_DIR", "").strip()
    if not override:
        return None
    candidate = Path(override).expanduser()
    if candidate.is_absolute():
        return candidate
    return project_root / candidate if project_root is not None else None


def environment_runtime_root(
    project_root: Path | None, environ: Mapping[str, str] | None = None
) -> Path | None:
    """The runtime root an environment names: the ``_MERIDIAN_RUNTIME_DIR`` override, else the
    root derived from its project. The one rule for ``ResolvedContext`` and the caller's
    identity."""

    override = runtime_override_root(project_root, environ)
    if override is not None:
        return override
    return derive_runtime_root_from_project(project_root) if project_root is not None else None


def runtime_root_project_id(runtime_root: Path) -> str | None:
    """The committed project ID a runtime root belongs to, from where the root is.

    A root is ``projects/<id>``, and an ID may hold a slash (``team/proj`` is the root
    ``projects/team/proj``), so the ID is the whole path under ``projects/``, never the
    directory's name. ``None`` for an ad-hoc root, whose ID is a hash of a path on this
    machine and names no project anywhere else, and for a root outside ``projects/`` (a
    ``_MERIDIAN_RUNTIME_DIR`` override).
    """

    projects = (get_user_home() / "projects").resolve()
    try:
        relative = runtime_root.resolve().relative_to(projects)
    except ValueError:
        return None
    project_id = relative.as_posix()
    if not relative.parts or project_id.startswith(ADHOC_PROJECT_ID_PREFIX):
        return None
    return project_id


__all__ = [
    "derive_runtime_root_from_project",
    "environment_runtime_root",
    "root_has_runtime_state",
    "runtime_override_root",
    "runtime_root_project_id",
]
