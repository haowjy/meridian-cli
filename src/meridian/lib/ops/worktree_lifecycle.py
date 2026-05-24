"""High-level git worktree lifecycle coordination.

This module owns worktree policy around creation, reopen restoration, cleanup,
and crash recovery. It is intentionally limited to git/worktree concerns and
returns structured results for callers to persist and present elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from meridian.lib.config.settings import WorkConfig, load_config
from meridian.lib.ops.worktree_ops import (
    WorktreeError,
    WorktreeMoveFailed,
    WorktreeRepoResolutionError,
    create_worktree,
    current_worktree_branch,
    detect_git_repo,
    ensure_no_unpushed_commits,
    move_worktree,
    remove_worktree,
    resolve_main_repo_root,
    resolve_worktree_path,
    worktree_exists,
)
from meridian.lib.state.work_store import WorkItem, WorktreeMetadata

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WorktreeProvisionResult:
    status: Literal["provisioned", "skipped_not_git_repo"]
    metadata: WorktreeMetadata
    created: bool = False


@dataclass(frozen=True)
class WorktreeRestoreResult:
    status: Literal[
        "not_configured",
        "manual_assignment",
        "manual_missing",
        "available",
        "restored",
        "fallback_project_root",
        "failed",
        "branch_missing",
    ]
    metadata: WorktreeMetadata
    error: str | None = None


@dataclass(frozen=True)
class WorktreeCleanupResult:
    status: Literal[
        "not_configured",
        "skipped_manual",
        "shared_reference",
        "missing",
        "validated",
        "removed",
        "failed",
    ]
    metadata: WorktreeMetadata
    forced: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorktreeRecoveryResult:
    status: Literal["healed", "cleared"]
    metadata: WorktreeMetadata


@dataclass(frozen=True)
class WorktreeRenameResult:
    status: Literal[
        "not_configured",
        "skipped_manual",
        "shared_reference",
        "missing",
        "renamed",
        "failed",
    ]
    metadata: WorktreeMetadata
    error: str | None = None


def default_worktree_branch(work_slug: str) -> str:
    return f"feature/{work_slug}"


def _target_metadata(
    *,
    repo_root: Path,
    work_slug: str,
    config: WorkConfig,
    existing: WorktreeMetadata | None = None,
) -> WorktreeMetadata:
    current = existing or WorktreeMetadata()
    worktree_name = current.name or work_slug
    path = current.path or str(
        resolve_worktree_path(repo_root, worktree_name, config.worktree_base)
    )
    branch = current.branch or default_worktree_branch(work_slug)
    return WorktreeMetadata(
        path=path,
        branch=branch,
        repo_path=str(repo_root),
        name=worktree_name,
        pending=False,
        managed=True,
    )


def provision_for_start(
    project_root: Path,
    work_slug: str,
    config: WorkConfig,
    *,
    existing: WorktreeMetadata | None = None,
) -> WorktreeProvisionResult:
    """Create or reuse the worktree for a started work item."""
    if not detect_git_repo(project_root):
        metadata = existing or WorktreeMetadata(branch=default_worktree_branch(work_slug))
        return WorktreeProvisionResult(
            status="skipped_not_git_repo",
            metadata=metadata.model_copy(update={"pending": False, "managed": True}),
            created=False,
        )

    repo_root = resolve_main_repo_root(project_root)
    if repo_root is None:
        raise WorktreeRepoResolutionError(
            f"Could not determine git repository root from '{project_root}'."
        )

    target = _target_metadata(
        repo_root=repo_root,
        work_slug=work_slug,
        config=config,
        existing=existing,
    )
    target_path = target.path or str(
        resolve_worktree_path(repo_root, target.name or work_slug, config.worktree_base)
    )
    result = create_worktree(
        repo_root,
        Path(target_path),
        target.branch or default_worktree_branch(work_slug),
    )
    return WorktreeProvisionResult(
        status="provisioned",
        metadata=WorktreeMetadata(
            path=str(result.path),
            branch=result.branch,
            repo_path=target.repo_path,
            name=target.name,
            pending=False,
            managed=True,
        ),
        created=result.created,
    )


def restore_for_reopen(project_root: Path, item: WorkItem) -> WorktreeRestoreResult:
    """Restore a previously provisioned worktree for a reopened item."""
    if item.worktree_path is None:
        return WorktreeRestoreResult(status="not_configured", metadata=item.worktree)

    worktree_path = Path(item.worktree_path)
    if not item.worktree_managed:
        if worktree_path.is_dir():
            return WorktreeRestoreResult(status="manual_assignment", metadata=item.worktree)
        return WorktreeRestoreResult(status="manual_missing", metadata=item.worktree)

    if worktree_path.is_dir():
        return WorktreeRestoreResult(status="available", metadata=item.worktree)

    if not item.worktree_branch:
        return WorktreeRestoreResult(status="branch_missing", metadata=item.worktree)

    main_root = resolve_main_repo_root(project_root)
    if main_root is None:
        return WorktreeRestoreResult(status="fallback_project_root", metadata=item.worktree)

    try:
        result = create_worktree(main_root, worktree_path, item.worktree_branch)
    except WorktreeError as exc:
        logger.warning(
            "worktree_lifecycle.restore_for_reopen.failed",
            work_id=item.name,
            worktree_path=str(worktree_path),
            error=str(exc),
        )
        return WorktreeRestoreResult(status="failed", metadata=item.worktree, error=str(exc))

    return WorktreeRestoreResult(
        status="restored",
        metadata=WorktreeMetadata(
            path=str(result.path),
            branch=result.branch,
            pending=False,
            managed=True,
        ),
    )


def cleanup_for_done(
    project_root: Path,
    item: WorkItem,
    *,
    force: bool = False,
    remove: bool = True,
    shared_with: tuple[str, ...] = (),
) -> WorktreeCleanupResult:
    """Remove a worktree for a completed item, guarding against unpushed work."""
    if item.worktree_path is None:
        return WorktreeCleanupResult(status="not_configured", metadata=item.worktree, forced=force)
    if not item.worktree_managed:
        return WorktreeCleanupResult(status="skipped_manual", metadata=item.worktree, forced=force)
    if shared_with:
        return WorktreeCleanupResult(
            status="shared_reference",
            metadata=item.worktree,
            forced=force,
            error=", ".join(shared_with),
        )

    worktree_path = Path(item.worktree_path)
    if not worktree_path.is_dir():
        return WorktreeCleanupResult(status="missing", metadata=item.worktree, forced=force)

    if not force:
        ensure_no_unpushed_commits(worktree_path)

    repo_root = resolve_main_repo_root(project_root) or resolve_main_repo_root(worktree_path)
    if repo_root is None:
        raise WorktreeRepoResolutionError(
            f"Could not determine git repository root for worktree at '{worktree_path}'."
        )

    if not remove:
        return WorktreeCleanupResult(status="validated", metadata=item.worktree, forced=force)

    remove_worktree(repo_root, worktree_path, force=force)
    return WorktreeCleanupResult(status="removed", metadata=item.worktree, forced=force)


def cleanup_for_delete(
    project_root: Path,
    item: WorkItem,
    *,
    shared_with: tuple[str, ...] = (),
) -> WorktreeCleanupResult:
    """Remove a worktree for a deleted item without blocking on dirty state."""
    if item.worktree_path is None:
        return WorktreeCleanupResult(status="not_configured", metadata=item.worktree, forced=True)
    if not item.worktree_managed:
        return WorktreeCleanupResult(status="skipped_manual", metadata=item.worktree, forced=True)
    if shared_with:
        return WorktreeCleanupResult(
            status="shared_reference",
            metadata=item.worktree,
            forced=True,
            error=", ".join(shared_with),
        )

    worktree_path = Path(item.worktree_path)
    if not worktree_path.is_dir():
        return WorktreeCleanupResult(status="missing", metadata=item.worktree, forced=True)

    repo_root = resolve_main_repo_root(project_root) or resolve_main_repo_root(worktree_path)
    if repo_root is None:
        return WorktreeCleanupResult(
            status="failed",
            metadata=item.worktree,
            forced=True,
            error="could not determine git repo root",
        )

    try:
        remove_worktree(repo_root, worktree_path, force=True)
    except WorktreeError as exc:
        logger.warning(
            "worktree_lifecycle.cleanup_for_delete.failed",
            work_id=item.name,
            worktree_path=str(worktree_path),
            error=str(exc),
        )
        return WorktreeCleanupResult(
            status="failed",
            metadata=item.worktree,
            forced=True,
            error=str(exc),
        )
    return WorktreeCleanupResult(status="removed", metadata=item.worktree, forced=True)


def rename_worktree(
    project_root: Path,
    item: WorkItem,
    new_slug: str,
    config: WorkConfig,
    *,
    shared_with: tuple[str, ...] = (),
) -> WorktreeRenameResult:
    """Move a worktree path and rename its branch for a renamed work item."""
    if item.worktree_path is None:
        return WorktreeRenameResult(status="not_configured", metadata=item.worktree)
    if not item.worktree_managed:
        return WorktreeRenameResult(status="skipped_manual", metadata=item.worktree)
    if shared_with:
        return WorktreeRenameResult(
            status="shared_reference",
            metadata=item.worktree,
            error=", ".join(shared_with),
        )

    repo_root = resolve_main_repo_root(project_root)
    if repo_root is None:
        return WorktreeRenameResult(
            status="failed",
            metadata=item.worktree,
            error=f"Could not determine git repository root from '{project_root}'.",
        )

    new_path = resolve_worktree_path(repo_root, new_slug, config.worktree_base)
    new_branch = default_worktree_branch(new_slug)
    old_path = Path(item.worktree_path)
    if not old_path.is_dir():
        return WorktreeRenameResult(
            status="missing",
            metadata=item.worktree,
            error=f"Worktree directory not found at {old_path}",
        )

    if item.worktree_branch:
        old_branch = item.worktree_branch
    else:
        try:
            old_branch = current_worktree_branch(old_path)
        except WorktreeError as exc:
            return WorktreeRenameResult(status="failed", metadata=item.worktree, error=str(exc))

    try:
        result = move_worktree(repo_root, old_path, new_path, old_branch, new_branch)
    except WorktreeMoveFailed as exc:
        logger.warning(
            "worktree_lifecycle.rename_worktree.failed",
            work_id=item.name,
            old_path=str(old_path),
            new_path=str(new_path),
            old_branch=old_branch,
            new_branch=new_branch,
            error=str(exc),
        )
        return WorktreeRenameResult(status="failed", metadata=item.worktree, error=str(exc))

    return WorktreeRenameResult(
        status="renamed",
        metadata=WorktreeMetadata(
            path=str(result.new_path),
            branch=result.new_branch,
            pending=False,
            managed=True,
        ),
    )


def recover_pending(project_root: Path, item: WorkItem) -> WorktreeRecoveryResult:
    """Heal or clear an interrupted worktree-create marker."""
    if not item.worktree_managed:
        return WorktreeRecoveryResult(
            status="cleared",
            metadata=item.worktree.model_copy(update={"pending": False}),
        )

    config = load_config(project_root)
    branch = item.worktree_branch or default_worktree_branch(item.name)
    current_path = item.worktree_path
    worktree_name = item.worktree.name or item.name

    main_root = resolve_main_repo_root(project_root)
    if main_root is None:
        return WorktreeRecoveryResult(
            status="cleared",
            metadata=WorktreeMetadata(
                path=current_path,
                branch=branch,
                repo_path=item.worktree.repo_path,
                name=worktree_name,
                pending=False,
                managed=True,
            ),
        )

    expected_path = (
        Path(current_path)
        if current_path
        else resolve_worktree_path(
            main_root,
            worktree_name,
            config.work.worktree_base,
        )
    )
    if worktree_exists(expected_path):
        return WorktreeRecoveryResult(
            status="healed",
            metadata=WorktreeMetadata(
                path=str(expected_path.resolve()),
                branch=branch,
                repo_path=str(main_root),
                name=worktree_name,
                pending=False,
                managed=True,
            ),
        )
    return WorktreeRecoveryResult(
        status="cleared",
        metadata=WorktreeMetadata(
            path=current_path,
            branch=branch,
            repo_path=item.worktree.repo_path,
            name=worktree_name,
            pending=False,
            managed=True,
        ),
    )


__all__ = [
    "WorktreeCleanupResult",
    "WorktreeProvisionResult",
    "WorktreeRecoveryResult",
    "WorktreeRenameResult",
    "WorktreeRestoreResult",
    "cleanup_for_delete",
    "cleanup_for_done",
    "default_worktree_branch",
    "provision_for_start",
    "recover_pending",
    "rename_worktree",
    "restore_for_reopen",
]
