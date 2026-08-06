"""Shared task-directory resolution policy for spawn launches."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from meridian.lib.launch.request import LaunchCompositionSurface
from meridian.lib.state import work_store
from meridian.lib.state.spawn_scope import read_spawn_scope


@dataclass(frozen=True)
class EffectiveTaskDir:
    """Resolved task-dir for the current process (not a child launch)."""

    task_dir: Path
    source: Literal["scope", "work-item", "inherited", "project-root"]
    warning: str | None = None


@dataclass(frozen=True)
class TaskCwdResolution:
    """Resolved task-directory decision for one spawn."""

    task_cwd: Path
    source: str
    work_item: str | None
    warning: str | None = None


class WorkTaskDirMissing(Exception):
    """No usable fallback exists for a stale work-item task directory."""


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
        _ = surface
        return self.has_distinct_task_cwd


def _active_task_dir_for_item(
    *,
    project_state_dir: Path,
    work_id: str,
) -> Path | None:
    item = work_store.get_active_work_item(
        project_state_dir,
        work_id,
    )
    if item is None or item.task_dir is None:
        return None
    return Path(item.task_dir).expanduser().resolve()


def _stale_work_task_dir_message(
    *,
    task_dir: Path,
    work_id: str,
) -> str:
    return (
        f"Work item '{work_id}' has a configured task_dir that does not exist: "
        f"{task_dir}."
    )


def _fallback_warning(message: str | None, destination: Path) -> str | None:
    if message is None:
        return None
    return f"{message} Falling back to {destination} (work state unchanged)."


def _require_fallback_root(project_root: Path, message: str) -> Path:
    if project_root.is_dir():
        return project_root
    raise WorkTaskDirMissing(
        f"{message}\nNo fallback project directory exists: {project_root}"
    )


def _validated_explicit_task_dir(task_dir: Path) -> Path:
    if task_dir.exists() and task_dir.is_dir():
        return task_dir
    if not task_dir.exists():
        raise ValueError(f"task_dir does not exist: {task_dir}")
    raise ValueError(f"task_dir is not a directory: {task_dir}")


def _validated_inherited_task_dir(task_dir: Path) -> Path:
    resolved = task_dir.expanduser().resolve()
    if resolved.is_dir():
        return resolved
    raise ValueError(
        f"Inherited MERIDIAN_TASK_DIR does not exist: {resolved}\n"
        "Use --task-dir to set a valid directory, or meridian task-dir clear to reset "
        "past the stale bootstrap value."
    )


def _validated_scope_task_dir(task_dir: Path) -> Path:
    resolved = task_dir.expanduser().resolve()
    if resolved.is_dir():
        return resolved
    raise ValueError(f"Spawn scope task_dir does not exist: {resolved}")


def resolve_effective_task_dir(
    *,
    project_root: Path,
    project_state_dir: Path,
    spawn_id: str | None = None,
    inherited_task_dir: Path | None = None,
    work_id: str | None = None,
) -> EffectiveTaskDir:
    """Resolve where the current process edits source code."""

    resolved_project_root = project_root.expanduser().resolve()
    normalized_spawn_id = (spawn_id or "").strip() or None
    normalized_work_id = (work_id or "").strip() or None

    if normalized_spawn_id is not None:
        scope = read_spawn_scope(resolved_project_root, normalized_spawn_id)
        if scope.task_dir is not None:
            return EffectiveTaskDir(
                task_dir=_validated_scope_task_dir(scope.task_dir),
                source="scope",
            )
        skip_inherited = scope.task_dir_cleared
    else:
        skip_inherited = False

    if normalized_work_id is not None:
        work_path = _active_task_dir_for_item(
            project_state_dir=project_state_dir,
            work_id=normalized_work_id,
        )
        if work_path is not None:
            if work_path.is_dir():
                return EffectiveTaskDir(task_dir=work_path, source="work-item")
            stale_message = _stale_work_task_dir_message(
                task_dir=work_path,
                work_id=normalized_work_id,
            )
        else:
            stale_message = None
    else:
        stale_message = None

    if not skip_inherited and inherited_task_dir is not None:
        if stale_message is None:
            resolved_inherited = _validated_inherited_task_dir(inherited_task_dir)
            return EffectiveTaskDir(task_dir=resolved_inherited, source="inherited")
        resolved_inherited = inherited_task_dir.expanduser().resolve()
        if resolved_inherited.is_dir():
            return EffectiveTaskDir(
                task_dir=resolved_inherited,
                source="inherited",
                warning=_fallback_warning(stale_message, resolved_inherited),
            )

    return EffectiveTaskDir(
        task_dir=(
            _require_fallback_root(resolved_project_root, stale_message)
            if stale_message is not None
            else resolved_project_root
        ),
        source="project-root",
        warning=_fallback_warning(stale_message, resolved_project_root),
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _infer_caller_cwd_outside_project(
    caller_cwd: Path | None,
    authority_root: Path,
) -> Path | None:
    if caller_cwd is None:
        return None
    resolved_caller = caller_cwd.expanduser().resolve()
    resolved_authority = authority_root.resolve()
    if resolved_caller == resolved_authority:
        return None
    if _path_is_within(resolved_caller, resolved_authority):
        return None
    if not resolved_caller.is_dir():
        return None
    return resolved_caller


def resolve_task_cwd(
    authority_root: Path,
    *,
    project_state_dir: Path,
    explicit_task_dir: str | Path | None = None,
    explicit_work_id: str | None = None,
    inherited_task_dir: str | Path | None = None,
    ambient_work_id: str | None = None,
    caller_cwd: str | Path | None = None,
) -> TaskCwdResolution:
    """Resolve task directory used for references and task instructions.

    Resolution priority:
      1. explicit task-dir override
      2. explicit work item task_dir
      3. inherited task-dir from parent
      4. ambient work item task_dir
      5. caller cwd when outside the project tree
      6. authority root default
    """

    resolved_authority_root = authority_root.resolve()
    explicit_override = (str(explicit_task_dir).strip() if explicit_task_dir is not None else "")
    if explicit_override:
        selected_work_id = (
            (explicit_work_id or "").strip() or (ambient_work_id or "").strip() or None
        )
        explicit_task_dir_path = _validated_explicit_task_dir(
            Path(explicit_override).expanduser().resolve()
        )
        return TaskCwdResolution(
            task_cwd=explicit_task_dir_path,
            source="explicit-task-dir",
            work_item=selected_work_id,
        )

    selected_work_id = (explicit_work_id or "").strip() or None
    ambient_selected_work_id = (ambient_work_id or "").strip() or None

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
        if explicit_path.is_dir():
            return TaskCwdResolution(
                task_cwd=explicit_path,
                source="explicit-work-task-dir",
                work_item=selected_work_id,
            )
        stale_message = _stale_work_task_dir_message(
            task_dir=explicit_path,
            work_id=selected_work_id,
        )
    else:
        stale_message = None

    inherited_override = (
        str(inherited_task_dir).strip() if inherited_task_dir is not None else ""
    )
    if inherited_override:
        inherited_candidate = Path(inherited_override).expanduser().resolve()
        if stale_message is None:
            inherited_path = _validated_inherited_task_dir(inherited_candidate)
        elif inherited_candidate.is_dir():
            inherited_path = inherited_candidate
        else:
            inherited_path = None
        if inherited_path is not None:
            return TaskCwdResolution(
                task_cwd=inherited_path,
                source="inherited-task-dir",
                work_item=selected_work_id or ambient_selected_work_id,
                warning=_fallback_warning(stale_message, inherited_path),
            )

    if selected_work_id is None and ambient_selected_work_id is not None:
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
        if ambient_path.is_dir():
            return TaskCwdResolution(
                task_cwd=ambient_path,
                source="ambient-work-task-dir",
                work_item=ambient_selected_work_id,
            )
        stale_message = _stale_work_task_dir_message(
            task_dir=ambient_path,
            work_id=ambient_selected_work_id,
        )

    inferred_caller_cwd = _infer_caller_cwd_outside_project(
        Path(caller_cwd) if caller_cwd is not None else None,
        resolved_authority_root,
    )
    if inferred_caller_cwd is not None:
        return TaskCwdResolution(
            task_cwd=inferred_caller_cwd,
            source="ambient-cwd",
            work_item=selected_work_id or ambient_selected_work_id,
            warning=_fallback_warning(stale_message, inferred_caller_cwd),
        )

    return TaskCwdResolution(
        task_cwd=(
            _require_fallback_root(resolved_authority_root, stale_message)
            if stale_message is not None
            else resolved_authority_root
        ),
        source=(
            "explicit-work-authority-root"
            if selected_work_id is not None
            else (
                "ambient-work-authority-root"
                if ambient_selected_work_id is not None
                else "authority-root"
            )
        ),
        work_item=selected_work_id or ambient_selected_work_id,
        warning=_fallback_warning(stale_message, resolved_authority_root),
    )
