"""Shared child-process CWD policy for spawn launches."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from meridian.lib.launch.request import LaunchCompositionSurface
from meridian.lib.state import work_store


@dataclass(frozen=True)
class TaskCwdResolution:
    """Resolved task working-directory decision for one spawn."""

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
            actual_process_cwd=resolved_task_cwd,
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
        """Whether launch composition should tell the agent to cd into task cwd.

        Child spawn tool cwd can vary by harness even when Meridian requests the
        task cwd. Be conservative for all non-primary child launches with a
        distinct task cwd; keep primary sessions quiet unless a future primary
        flow needs explicit task-cwd steering.
        """

        return self.has_distinct_task_cwd and surface != LaunchCompositionSurface.PRIMARY


def _active_worktree_path_for_item(
    *,
    project_state_dir: Path,
    work_id: str,
) -> Path | None:
    item = work_store.get_active_work_item(project_state_dir, work_id)
    if item is None or item.worktree_path is None:
        return None
    return Path(item.worktree_path).expanduser().resolve()


def _validated_worktree_path(
    *,
    worktree_path: Path,
    work_id: str,
) -> Path:
    if worktree_path.is_dir():
        return worktree_path
    raise ValueError(
        f"Work item '{work_id}' has a configured worktree path that does not exist.\n"
        f"  worktree_path: {worktree_path}\n"
        "Use --no-worktree to launch from the authority root."
    )


def resolve_task_cwd(
    authority_root: Path,
    *,
    project_state_dir: Path,
    explicit_work_id: str | None = None,
    ambient_work_id: str | None = None,
    force_worktree: bool = False,
    force_no_worktree: bool = False,
) -> TaskCwdResolution:
    """Resolve task cwd from work/worktree intent.

    Resolution priority:
      1. --no-worktree
      2. --worktree
      3. explicit --work boundary
      4. ambient work attachment
      5. authority root default
    """

    resolved_authority_root = authority_root.resolve()
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
        configured_path = _active_worktree_path_for_item(
            project_state_dir=project_state_dir,
            work_id=force_worktree_work_id,
        )
        if configured_path is None:
            raise ValueError(
                f"--worktree requested, but work item '{force_worktree_work_id}' "
                "has no configured worktree path."
            )
        return TaskCwdResolution(
            task_cwd=_validated_worktree_path(
                worktree_path=configured_path,
                work_id=force_worktree_work_id,
            ),
            source="forced-worktree",
            work_item=force_worktree_work_id,
        )

    if selected_work_id is not None:
        explicit_path = _active_worktree_path_for_item(
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
            task_cwd=_validated_worktree_path(
                worktree_path=explicit_path,
                work_id=selected_work_id,
            ),
            source="explicit-work-worktree",
            work_item=selected_work_id,
        )

    if ambient_selected_work_id is not None:
        ambient_path = _active_worktree_path_for_item(
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
            task_cwd=_validated_worktree_path(
                worktree_path=ambient_path,
                work_id=ambient_selected_work_id,
            ),
            source="ambient-work-worktree",
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

    if worktree_path is not None:
        return _validated_worktree_path(
            worktree_path=worktree_path.expanduser().resolve(),
            work_id=work_id or "<unknown>",
        )
    if project_state_dir is None:
        return project_root.resolve()
    resolution = resolve_task_cwd(
        project_root,
        project_state_dir=project_state_dir,
        explicit_work_id=work_id,
    )
    return resolution.task_cwd
