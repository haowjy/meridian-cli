"""Runtime state root derivation from project identity (read-only)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.state.project_identity import (
    ensure_project_identity,
    resolve_project_identity,
    state_id,
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
    """Return the explicit ``_MERIDIAN_RUNTIME_DIR`` runtime root, at any spawn depth.

    The one rule for the override; ``ops/runtime.py`` and ``ResolvedContext`` both
    call it. Launchers set the variable on a child only when their own runtime came
    from an override (``launch/context.py``), so a value present is a deliberate
    choice. ``-C`` to another project unsets it at process scope, and hooks and
    ``BLOCKED_CHILD_ENV_VARS`` strip it from foreign envs. A relative value needs
    *project_root* to anchor it.
    """

    env = os.environ if environ is None else environ
    override = env.get("_MERIDIAN_RUNTIME_DIR", "").strip()
    if not override:
        return None
    candidate = Path(override).expanduser()
    if candidate.is_absolute():
        return candidate
    return project_root / candidate if project_root is not None else None


__all__ = ["derive_runtime_root_from_project", "root_has_runtime_state", "runtime_override_root"]
