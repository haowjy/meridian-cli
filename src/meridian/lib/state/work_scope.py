"""Explicit work-scope model: named work items vs ambient spawn-local scratch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import (
    resolve_ambient_work_dir,
    resolve_work_scratch_dir_for_project,
)

_SCOPE_STATUS_FILENAME = "__status.json"
SCOPE_PROMPTS_DIRNAME = "prompts"
SCOPE_HANDOFFS_DIRNAME = "handoffs"

WorkScopeKind = Literal["work_item", "ambient_spawn"]


@dataclass(frozen=True)
class WorkScope:
    """Resolved active work scope with explicit persistence semantics."""

    kind: WorkScopeKind
    identifier: str
    root: Path

    @property
    def is_durable(self) -> bool:
        return self.kind == "work_item"

    @property
    def is_ephemeral(self) -> bool:
        return self.kind == "ambient_spawn"

    @property
    def excluded_top_level_names(self) -> frozenset[str]:
        """Top-level scope entries that never count as user artifacts."""

        return frozenset({_SCOPE_STATUS_FILENAME, SCOPE_PROMPTS_DIRNAME})

    def count_artifacts(self) -> int:
        """Count keepable artifacts under this scope (pure read, no mkdir)."""

        root = self.root
        if not root.is_dir():
            return 0

        excluded = self.excluded_top_level_names
        count = 0
        for child in root.iterdir():
            name = child.name
            if name in excluded:
                continue
            if name == SCOPE_HANDOFFS_DIRNAME:
                if child.is_dir():
                    count += sum(1 for _ in child.iterdir())
                else:
                    count += 1
                continue
            count += 1
        return count


def resolve_bound_work_scope(
    *,
    project_root: Path,
    requested_work_id: str | None,
    child_spawn_id: str,
    runtime_root: Path,
) -> WorkScope | None:
    """Bind-time scope: named repo scratch or child ambient spawn dir."""

    if requested_work_id is not None:
        try:
            root = resolve_work_scratch_dir_for_project(project_root, requested_work_id)
        except ValueError:
            return None
        return WorkScope(kind="work_item", identifier=requested_work_id, root=root)
    root = resolve_ambient_work_dir(
        project_root, child_spawn_id, runtime_root=runtime_root
    )
    return WorkScope(kind="ambient_spawn", identifier=child_spawn_id, root=root)


def resolve_work_scope_from_parts(
    *,
    project_root: Path | None,
    spawn_id: SpawnId | None,
    work_id: str | None,
    bound_work_dir: Path | None,
    project_work_dir: Path | None = None,
    runtime_root: Path | None = None,
) -> WorkScope | None:
    """Resolve active work scope from already-derived context parts.

    *bound_work_dir* is the env-bound directory when ``MERIDIAN_ACTIVE_WORK_DIR``
    wins over canonical resolution; *project_work_dir* is the repo-scoped work
    root used for named items when available.
    """

    if bound_work_dir is not None:
        if work_id:
            return WorkScope(kind="work_item", identifier=work_id, root=bound_work_dir)
        if spawn_id is not None:
            return WorkScope(kind="ambient_spawn", identifier=str(spawn_id), root=bound_work_dir)
        return WorkScope(kind="ambient_spawn", identifier="", root=bound_work_dir)

    if work_id:
        if project_work_dir is not None:
            root = project_work_dir / work_id
        elif project_root is not None:
            try:
                root = resolve_work_scratch_dir_for_project(project_root, work_id)
            except ValueError:
                return None
        else:
            return None
        return WorkScope(kind="work_item", identifier=work_id, root=root)

    if spawn_id is not None and project_root is not None:
        try:
            root = resolve_ambient_work_dir(
                project_root, spawn_id, runtime_root=runtime_root
            )
        except ValueError:
            return None
        return WorkScope(kind="ambient_spawn", identifier=str(spawn_id), root=root)

    return None


__all__ = [
    "SCOPE_HANDOFFS_DIRNAME",
    "SCOPE_PROMPTS_DIRNAME",
    "WorkScope",
    "WorkScopeKind",
    "resolve_bound_work_scope",
    "resolve_work_scope_from_parts",
]
