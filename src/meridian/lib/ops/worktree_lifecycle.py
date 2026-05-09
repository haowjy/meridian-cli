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
    WorktreeRepoResolutionError,
    create_worktree,
    detect_git_repo,
    ensure_no_unpushed_commits,
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
    status: Literal["not_configured", "missing", "validated", "removed", "failed"]
    metadata: WorktreeMetadata
    forced: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorktreeRecoveryResult:
    status: Literal["healed", "cleared"]
    metadata: WorktreeMetadata


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
    path = current.path or str(resolve_worktree_path(repo_root, work_slug, config.worktree_base))
    branch = current.branch or default_worktree_branch(work_slug)
    return WorktreeMetadata(path=path, branch=branch, pending=False)


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
            metadata=metadata.model_copy(update={"pending": False}),
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
    target_path = target.path or str(resolve_worktree_path(repo_root, work_slug, config.worktree_base))
    result = create_worktree(
        repo_root,
        Path(target_path),
        target.branch or default_worktree_branch(work_slug),
    )
    return WorktreeProvisionResult(
        status="provisioned",
        metadata=WorktreeMetadata(path=str(result.path), branch=result.branch, pending=False),
        created=result.created,
    )


def restore_for_reopen(project_root: Path, item: WorkItem) -> WorktreeRestoreResult:
    """Restore a previously provisioned worktree for a reopened item."""
    if item.worktree_path is None:
        return WorktreeRestoreResult(status="not_configured", metadata=item.worktree)

    worktree_path = Path(item.worktree_path)
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
        metadata=WorktreeMetadata(path=str(result.path), branch=result.branch, pending=False),
    )


def cleanup_for_done(
    project_root: Path,
    item: WorkItem,
    *,
    force: bool = False,
    remove: bool = True,
) -> WorktreeCleanupResult:
    """Remove a worktree for a completed item, guarding against unpushed work."""
    if item.worktree_path is None:
        return WorktreeCleanupResult(status="not_configured", metadata=item.worktree, forced=force)

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


def cleanup_for_delete(project_root: Path, item: WorkItem) -> WorktreeCleanupResult:
    """Remove a worktree for a deleted item without blocking on dirty state."""
    if item.worktree_path is None:
        return WorktreeCleanupResult(status="not_configured", metadata=item.worktree, forced=True)

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


def recover_pending(project_root: Path, item: WorkItem) -> WorktreeRecoveryResult:
    """Heal or clear an interrupted worktree-create marker."""
    config = load_config(project_root)
    branch = item.worktree_branch or default_worktree_branch(item.name)
    current_path = item.worktree_path

    main_root = resolve_main_repo_root(project_root)
    if main_root is None:
        return WorktreeRecoveryResult(
            status="cleared",
            metadata=WorktreeMetadata(path=current_path, branch=branch, pending=False),
        )

    expected_path = Path(current_path) if current_path else resolve_worktree_path(
        main_root,
        item.name,
        config.work.worktree_base,
    )
    if worktree_exists(expected_path):
        return WorktreeRecoveryResult(
            status="healed",
            metadata=WorktreeMetadata(
                path=str(expected_path.resolve()),
                branch=branch,
                pending=False,
            ),
        )
    return WorktreeRecoveryResult(
        status="cleared",
        metadata=WorktreeMetadata(path=current_path, branch=branch, pending=False),
    )


__all__ = [
    "WorktreeCleanupResult",
    "WorktreeProvisionResult",
    "WorktreeRecoveryResult",
    "WorktreeRestoreResult",
    "cleanup_for_delete",
    "cleanup_for_done",
    "default_worktree_branch",
    "provision_for_start",
    "recover_pending",
    "restore_for_reopen",
]
