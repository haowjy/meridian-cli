"""Worktree status/ensure command orchestration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_chat_id,
    resolve_roots,
    runtime_context,
)
from meridian.lib.ops.worktree_ensure import (
    WorktreeEnsureError,
    ensure_temporary_worktree,
    ensure_work_item_worktree,
    get_temporary_worktree_status,
)
from meridian.lib.state import session_store, work_store

_NESTED_WORK_WARNING = (
    "Work coordination is primary-owned; nested agents should usually ask the orchestrator "
    "to run this command."
)


class WorkWorktreeInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str = ""
    ensure: bool = False
    repo: str | None = None
    chat_id: str = ""
    project_root: str | None = None


class WorkWorktreeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_name: str | None = None
    repo_root: str | None = None
    managed: bool = False
    exists: bool = False
    temporary: bool = False
    ensured: bool = False
    message: str = ""
    warning: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = [self.message]
        if self.worktree_path:
            details = [self.worktree_path]
            if self.worktree_branch:
                details.append(self.worktree_branch)
            if self.worktree_name:
                details.append(f"name={self.worktree_name}")
            lines.append("🌳 " + " ".join(details))
        if self.repo_root:
            lines.append(f"Repo: {self.repo_root}")
        if self.warning:
            lines.append(self.warning)
        return "\n".join(line for line in lines if line)


def _work_warning(ctx: RuntimeContext | None) -> str | None:
    if runtime_context(ctx).is_nested:
        return _NESTED_WORK_WARNING
    return None


def _merge_warnings(*warnings: str | None) -> str | None:
    parts = [warning.strip() for warning in warnings if warning and warning.strip()]
    if not parts:
        return None
    return "\n".join(parts)


def work_worktree_sync(
    payload: WorkWorktreeInput,
    ctx: RuntimeContext | None = None,
) -> WorkWorktreeOutput:
    warning = _work_warning(ctx)
    roots = resolve_roots(payload.project_root)
    project_root = roots.project_root
    project_state_dir = roots.project_state_dir
    runtime_root = roots.runtime_root
    chat_id = resolve_chat_id(payload_chat_id=payload.chat_id, ctx=runtime_context(ctx))
    selected_work_id = payload.work_id.strip() or ""
    if not selected_work_id and chat_id:
        selected_work_id = session_store.get_session_active_work_id(runtime_root, chat_id) or ""
    item = None
    if selected_work_id:
        item = work_store.get_active_work_item(project_state_dir, selected_work_id)
        if item is None:
            archived = work_store.get_work_item(project_state_dir, selected_work_id)
            if archived is not None and archived.status == "done":
                raise ValueError(f"Work item '{selected_work_id}' is archived. Reopen it first.")
            raise ValueError(f"Work item '{selected_work_id}' not found.")
    if item is None:
        if payload.ensure:
            ensured_temp = ensure_temporary_worktree(
                project_root=project_root,
                runtime_root=runtime_root,
                ctx=ctx,
                target_repo=payload.repo,
                execution_cwd=roots.execution_cwd,
                dry_run=False,
            )
            worktree_path = ensured_temp.metadata.path or str(ensured_temp.canonical_path)
            return WorkWorktreeOutput(
                worktree_path=worktree_path,
                worktree_branch=ensured_temp.metadata.branch,
                worktree_name=ensured_temp.metadata.name,
                repo_root=ensured_temp.metadata.repo_path or ensured_temp.repo_root.as_posix(),
                managed=ensured_temp.metadata.managed,
                exists=Path(worktree_path).is_dir(),
                temporary=True,
                ensured=ensured_temp.status == "temporary_provisioned",
                message="Temporary worktree ready.",
                warning=_merge_warnings(warning, ensured_temp.warning),
            )
        temporary = get_temporary_worktree_status(runtime_root=runtime_root, ctx=ctx)
        if temporary is not None:
            worktree_path = temporary.metadata.path or str(temporary.canonical_path)
            return WorkWorktreeOutput(
                worktree_path=worktree_path,
                worktree_branch=temporary.metadata.branch,
                worktree_name=temporary.metadata.name,
                repo_root=temporary.metadata.repo_path or temporary.repo_root.as_posix(),
                managed=temporary.metadata.managed,
                exists=Path(worktree_path).is_dir(),
                temporary=True,
                ensured=False,
                message=(
                    "Temporary worktree provisioning pending."
                    if temporary.status == "temporary_pending"
                    else "Temporary worktree status."
                ),
                warning=_merge_warnings(warning, temporary.warning),
            )
        return WorkWorktreeOutput(
            message=(
                "No active work item and no tracked temporary worktree. "
                "Run `meridian work start <name>` or pass "
                "`meridian work worktree <work-id>`."
            ),
            warning=warning,
        )

    if payload.ensure:
        try:
            ensured = ensure_work_item_worktree(
                project_root=project_root,
                project_state_dir=project_state_dir,
                work_id=item.name,
                target_repo=payload.repo,
                execution_cwd=roots.execution_cwd,
                dry_run=False,
            )
        except WorktreeEnsureError as exc:
            raise ValueError(str(exc)) from exc

        worktree_path = ensured.metadata.path or str(ensured.canonical_path)
        branch = ensured.metadata.branch
        return WorkWorktreeOutput(
            work_id=item.name,
            worktree_path=worktree_path,
            worktree_branch=branch,
            worktree_name=ensured.metadata.name or item.name,
            repo_root=ensured.metadata.repo_path or ensured.repo_root.as_posix(),
            managed=ensured.metadata.managed,
            exists=Path(worktree_path).is_dir(),
            temporary=False,
            ensured=ensured.status in {"provisioned", "recovered"},
            message=f"Worktree ready for '{item.name}'.",
            warning=_merge_warnings(warning, ensured.warning),
        )

    worktree_path = item.worktree_path
    exists = Path(worktree_path).expanduser().resolve().is_dir() if worktree_path else False
    state = "configured" if worktree_path else "not configured"
    message = f"Worktree status for '{item.name}': {state}"
    if worktree_path and not exists:
        if item.worktree_managed:
            warning = _merge_warnings(
                warning,
                (
                    f"Managed worktree path is missing: {worktree_path}\n"
                    "Run `meridian work worktree --ensure` to recover it."
                ),
            )
        else:
            warning = _merge_warnings(
                warning,
                (
                    f"Manual worktree assignment is missing: {worktree_path}\n"
                    "Restore or clear the assignment, or use --no-worktree."
                ),
            )
    return WorkWorktreeOutput(
        work_id=item.name,
        worktree_path=worktree_path,
        worktree_branch=item.worktree_branch,
        worktree_name=item.worktree_name or item.name,
        repo_root=item.worktree_repo_path,
        managed=item.worktree_managed,
        exists=exists,
        temporary=False,
        ensured=False,
        message=message,
        warning=warning,
    )


work_worktree = async_from_sync(work_worktree_sync)


__all__ = [
    "WorkWorktreeInput",
    "WorkWorktreeOutput",
    "work_worktree",
    "work_worktree_sync",
]
