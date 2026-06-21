"""Shared task-dir derivation for spawn prepare and execute paths."""

from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.launch.cwd import resolve_effective_task_dir


def _normalized_inherited_task_dir_from_env() -> Path | None:
    inherited_task_dir_raw = os.getenv("MERIDIAN_TASK_DIR", "").strip()
    if not inherited_task_dir_raw:
        return None
    return Path(inherited_task_dir_raw).expanduser().resolve()


def derive_inheritable_task_dir(
    *,
    project_root: Path,
    project_state_dir: Path,
    spawn_id: str | None,
    work_id: str | None,
) -> Path | None:
    """Parent's child-inheritable task-dir: the genuinely inheritable surface
    (mutable scope file or inherited MERIDIAN_TASK_DIR env) only. Work-item and
    project-root provenance are left to resolve_task_cwd's own tiers so they are
    not shadowed."""

    inherited_env = _normalized_inherited_task_dir_from_env()
    effective = resolve_effective_task_dir(
        project_root=project_root,
        project_state_dir=project_state_dir,
        spawn_id=spawn_id,
        inherited_task_dir=inherited_env,
        work_id=work_id,
    )
    return effective.task_dir if effective.source in ("scope", "inherited") else None


__all__ = ["derive_inheritable_task_dir"]
