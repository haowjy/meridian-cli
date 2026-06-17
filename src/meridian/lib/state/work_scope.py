"""Explicit work-scope model: named work items vs ambient spawn-local scratch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import (
    resolve_ambient_work_dir,
    resolve_work_scratch_dir,
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

    def scope_label(self) -> str:
        """Human-readable label for this scope in user-facing messages."""

        if self.is_durable:
            return f"work item '{self.identifier}'"
        if self.identifier:
            return f"spawn-local work area ({self.identifier})"
        return "the current scope"

    def format_leave_scope_warning(self, new_target: str) -> str | None:
        """Return a leave-scope warning when artifacts would be left behind."""

        count = self.count_artifacts()
        if count <= 0:
            return None
        noun = "artifact" if count == 1 else "artifacts"
        return (
            f"{count} {noun} in {self.scope_label()} won't follow you to {new_target}; "
            "move what you want to keep."
        )


def resolve_bound_work_scope(
    *,
    project_root: Path,
    requested_work_id: str | None,
    child_spawn_id: str,
) -> WorkScope:
    """Bind-time scope: named repo scratch or child ambient spawn dir."""

    if requested_work_id is not None:
        root = resolve_work_scratch_dir_for_project(project_root, requested_work_id)
        return WorkScope(kind="work_item", identifier=requested_work_id, root=root)
    root = resolve_ambient_work_dir(project_root, child_spawn_id)
    return WorkScope(kind="ambient_spawn", identifier=child_spawn_id, root=root)


def resolve_work_scope_from_parts(
    *,
    project_root: Path | None,
    runtime_root: Path | None,
    spawn_id: SpawnId | None,
    work_id: str | None,
    bound_work_dir: Path | None,
    project_work_dir: Path | None = None,
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
        elif runtime_root is not None:
            root = resolve_work_scratch_dir(runtime_root, work_id)
        else:
            return None
        return WorkScope(kind="work_item", identifier=work_id, root=root)

    if spawn_id is not None and project_root is not None:
        root = resolve_ambient_work_dir(project_root, spawn_id)
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
