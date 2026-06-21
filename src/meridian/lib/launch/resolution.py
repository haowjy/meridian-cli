"""Shared pre-composition launch input resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.launch.cwd import LaunchDirectoryContext, TaskCwdResolution, resolve_task_cwd
from meridian.lib.launch.reference import validate_reference_paths
from meridian.lib.state.paths import resolve_kb_dir


@dataclass(frozen=True)
class LaunchInputResolution:
    """Resolved work, task cwd, and reference path surface shared by launch callers."""

    effective_work_id: str | None
    task_cwd_resolution: TaskCwdResolution
    directory_context: LaunchDirectoryContext
    reference_files: tuple[Path, ...]

    @property
    def request_updates(self) -> dict[str, str | None | tuple[str, ...]]:
        return {
            "work_id_hint": self.effective_work_id,
            "reference_files": tuple(path.as_posix() for path in self.reference_files),
            "authority_root": self.directory_context.authority_root.as_posix(),
            "task_cwd": self.directory_context.logical_task_cwd.as_posix(),
            "reference_anchor": self.directory_context.reference_anchor.as_posix(),
            "task_cwd_source": self.directory_context.task_cwd_source,
            "task_cwd_work_item": self.directory_context.work_item,
        }

    @property
    def runtime_updates(self) -> dict[str, str]:
        task_cwd = self.directory_context.logical_task_cwd.as_posix()
        return {
            "requested_task_cwd": task_cwd,
            "project_paths_execution_cwd": task_cwd,
        }


def _first_context_work_id(project_root: Path, refs: tuple[str, ...]) -> str | None:
    if not refs:
        return None
    from meridian.lib.ops.spawn.context_ref import first_context_ref_work_id

    return first_context_ref_work_id(project_root, refs)


def resolve_launch_inputs(
    *,
    authority_root: Path,
    project_state_dir: Path,
    context_from: tuple[str, ...],
    reference_files: tuple[str, ...],
    explicit_task_dir: str | Path | None = None,
    explicit_work_id: str | None = None,
    inherited_task_dir: str | Path | None = None,
    ambient_work_id: str | None = None,
    caller_cwd: str | Path | None = None,
    inherit_context_work: bool = True,
) -> LaunchInputResolution:
    """Resolve the shared launch surface before prompt composition.

    Work precedence is:
      1. explicit work
      2. ambient/session work
      3. first ``--from`` work when enabled
    """

    resolved_authority_root = authority_root.expanduser().resolve()
    normalized_explicit_work_id = (explicit_work_id or "").strip() or None
    normalized_ambient_work_id = (ambient_work_id or "").strip() or None
    context_work_id = None
    if (
        inherit_context_work
        and normalized_explicit_work_id is None
        and normalized_ambient_work_id is None
    ):
        context_work_id = _first_context_work_id(resolved_authority_root, context_from)

    effective_work_id = (
        normalized_explicit_work_id or normalized_ambient_work_id or context_work_id
    )
    task_explicit_work_id = normalized_explicit_work_id
    task_ambient_work_id = normalized_ambient_work_id
    if context_work_id is not None:
        task_ambient_work_id = context_work_id

    task_cwd_resolution = resolve_task_cwd(
        resolved_authority_root,
        project_state_dir=project_state_dir,
        explicit_task_dir=explicit_task_dir,
        explicit_work_id=task_explicit_work_id,
        inherited_task_dir=inherited_task_dir,
        ambient_work_id=task_ambient_work_id,
        caller_cwd=caller_cwd,
    )
    directory_context = LaunchDirectoryContext.from_task_cwd_resolution(
        authority_root=resolved_authority_root,
        task_cwd_resolution=task_cwd_resolution,
    )
    validated_reference_files = validate_reference_paths(
        reference_files,
        reference_anchor=directory_context.reference_anchor,
        kb_dir=resolve_kb_dir(resolved_authority_root),
    )

    return LaunchInputResolution(
        effective_work_id=effective_work_id,
        task_cwd_resolution=task_cwd_resolution,
        directory_context=directory_context,
        reference_files=validated_reference_files,
    )


__all__ = [
    "LaunchInputResolution",
    "resolve_launch_inputs",
]
