"""Shared task-directory resolution policy for spawn launches."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from meridian.lib.launch.request import LaunchCompositionSurface
from meridian.lib.state import work_store


@dataclass(frozen=True)
class TaskCwdResolution:
    """Resolved task-directory decision for one spawn."""

    task_cwd: Path
    source: str
    work_item: str | None


@dataclass(frozen=True)
class LaunchDirectoryContext:
    """Resolved directory contract for launch composition and execution."""

    authority_root: Path
    logical_task_cwd: Path
    reference_anchor: Path
    actual_process_cwd: Path
    task_cwd_source: str
    work_item: str | None

    @classmethod
    def from_task_cwd_resolution(
        cls,
        *,
        authority_root: Path,
        task_cwd_resolution: TaskCwdResolution,
    ) -> LaunchDirectoryContext:
        resolved_authority_root = authority_root.expanduser().resolve()
        resolved_task_cwd = task_cwd_resolution.task_cwd.expanduser().resolve()
        return cls(
            authority_root=resolved_authority_root,
            logical_task_cwd=resolved_task_cwd,
            reference_anchor=resolved_task_cwd,
            actual_process_cwd=resolved_authority_root,
            task_cwd_source=task_cwd_resolution.source,
            work_item=task_cwd_resolution.work_item,
        )

    def with_actual_process_cwd(self, actual_process_cwd: Path) -> LaunchDirectoryContext:
        return replace(
            self,
            actual_process_cwd=actual_process_cwd.expanduser().resolve(),
        )

    @property
    def has_distinct_task_cwd(self) -> bool:
        return self.logical_task_cwd != self.authority_root

    def should_inject_task_cwd_instruction(self, surface: LaunchCompositionSurface) -> bool:
        """Whether launch composition should tell the agent to cd into task cwd."""

        return self.has_distinct_task_cwd and surface != LaunchCompositionSurface.PRIMARY


def _active_task_dir_for_item(
    *,
    project_state_dir: Path,
    work_id: str,
) -> Path | None:
    item = work_store.get_active_work_item(project_state_dir, work_id)
    if item is None or item.task_dir is None:
        return None
    return Path(item.task_dir).expanduser().resolve()


def _validated_task_dir(
    *,
    task_dir: Path,
    work_id: str,
) -> Path:
    if task_dir.is_dir():
        return task_dir
    raise ValueError(
        f"Work item '{work_id}' has a configured task_dir that does not exist.\n"
        f"  task_dir: {task_dir}\n"
        "Use --task-dir to set a valid directory for this work item."
    )


def resolve_task_cwd(
    authority_root: Path,
    *,
    project_state_dir: Path,
    explicit_task_dir: str | Path | None = None,
    explicit_work_id: str | None = None,
    ambient_work_id: str | None = None,
    force_worktree: bool = False,
    force_no_worktree: bool = False,
) -> TaskCwdResolution:
    """Resolve task directory used for references and task instructions.

    Resolution priority:
      1. explicit task-dir override
      2. explicit work item task_dir
      3. ambient work item task_dir
      4. authority root default
    """

    resolved_authority_root = authority_root.resolve()
    explicit_override = (str(explicit_task_dir).strip() if explicit_task_dir is not None else "")
    if explicit_override:
        selected_work_id = (
            (explicit_work_id or "").strip() or (ambient_work_id or "").strip() or None
        )
        return TaskCwdResolution(
            task_cwd=Path(explicit_override).expanduser().resolve(),
            source="explicit-task-dir",
            work_item=selected_work_id,
        )

    if force_no_worktree:
        return TaskCwdResolution(
            task_cwd=resolved_authority_root,
            source="forced-no-worktree",
            work_item=None,
        )

    selected_work_id = (explicit_work_id or "").strip() or None
    ambient_selected_work_id = (ambient_work_id or "").strip() or None
    force_worktree_work_id = selected_work_id or ambient_selected_work_id

    if force_worktree:
        if force_worktree_work_id is None:
            raise ValueError(
                "--worktree requires a selected work item. "
                "Pass --work <item> or attach an active work item first."
            )
        configured_path = _active_task_dir_for_item(
            project_state_dir=project_state_dir,
            work_id=force_worktree_work_id,
        )
        if configured_path is None:
            raise ValueError(
                f"--worktree requested, but work item '{force_worktree_work_id}' "
                "has no configured task_dir."
            )
        return TaskCwdResolution(
            task_cwd=_validated_task_dir(
                task_dir=configured_path,
                work_id=force_worktree_work_id,
            ),
            source="forced-worktree",
            work_item=force_worktree_work_id,
        )

    if selected_work_id is not None:
        explicit_path = _active_task_dir_for_item(
            project_state_dir=project_state_dir,
            work_id=selected_work_id,
        )
        if explicit_path is None:
            return TaskCwdResolution(
                task_cwd=resolved_authority_root,
                source="explicit-work-authority-root",
                work_item=selected_work_id,
            )
        return TaskCwdResolution(
            task_cwd=_validated_task_dir(
                task_dir=explicit_path,
                work_id=selected_work_id,
            ),
            source="explicit-work-task-dir",
            work_item=selected_work_id,
        )

    if ambient_selected_work_id is not None:
        ambient_path = _active_task_dir_for_item(
            project_state_dir=project_state_dir,
            work_id=ambient_selected_work_id,
        )
        if ambient_path is None:
            return TaskCwdResolution(
                task_cwd=resolved_authority_root,
                source="ambient-work-authority-root",
                work_item=ambient_selected_work_id,
            )
        return TaskCwdResolution(
            task_cwd=_validated_task_dir(
                task_dir=ambient_path,
                work_id=ambient_selected_work_id,
            ),
            source="ambient-work-task-dir",
            work_item=ambient_selected_work_id,
        )

    return TaskCwdResolution(
        task_cwd=resolved_authority_root,
        source="authority-root",
        work_item=None,
    )


def resolve_child_execution_cwd(
    project_root: Path,
    *,
    project_state_dir: Path | None = None,
    work_id: str | None = None,
    worktree_path: Path | None = None,
) -> Path:
    """Legacy wrapper used by older call sites."""

    _ = (project_state_dir, work_id, worktree_path)
    return project_root.resolve()
