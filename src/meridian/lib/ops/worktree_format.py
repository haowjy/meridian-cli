"""Presentation helpers for worktree lifecycle results."""

from __future__ import annotations

from meridian.lib.ops.worktree_lifecycle import (
    WorktreeCleanupResult,
    WorktreeProvisionResult,
    WorktreeRecoveryResult,
    WorktreeRenameResult,
    WorktreeRestoreResult,
)


def format_provision_notice(result: WorktreeProvisionResult, *, work_id: str) -> str | None:
    if result.status == "skipped_not_git_repo":
        return f"Skipped worktree creation for '{work_id}': project is not inside a git repository."
    return None


def format_restore_notice(result: WorktreeRestoreResult) -> str | None:
    if result.status == "not_configured":
        return None
    if result.status == "manual_assignment":
        return None
    if result.status == "manual_missing":
        return (
            f"Assigned worktree path is missing: {result.metadata.path}\n"
            "Spawns will use the project root for CWD until the path exists."
        )
    if result.status == "available":
        return f"Worktree available at {result.metadata.path}"
    if result.status == "restored":
        return f"Recreated worktree at {result.metadata.path}"
    if result.status == "branch_missing":
        return "Worktree branch metadata is missing; spawns will use the project root for CWD."
    if result.status == "fallback_project_root":
        return (
            f"Worktree was removed: {result.metadata.path}\n"
            "Spawns will use the project root for CWD."
        )
    if result.status == "failed":
        return (
            f"Could not recreate worktree at '{result.metadata.path}': {result.error}\n"
            "Spawns will use the project root for CWD."
        )
    return None


def format_cleanup_notice(result: WorktreeCleanupResult) -> str | None:
    if result.status in {"not_configured", "missing", "validated"}:
        return None
    if result.status == "skipped_manual":
        return "Skipped worktree cleanup: path is manually assigned."
    if result.status == "shared_reference":
        return (
            f"Skipped worktree cleanup at {result.metadata.path}: "
            f"still referenced by work item(s): {result.error}"
        )
    if result.status == "removed":
        return f"Removed worktree at {result.metadata.path}"
    if result.status == "failed":
        return f"Warning: could not remove worktree at {result.metadata.path}: {result.error}"
    return None


def format_recovery_notice(result: WorktreeRecoveryResult) -> str | None:
    if result.status == "healed":
        return f"Recovered worktree metadata for {result.metadata.path}"
    return None


def format_rename_notice(result: WorktreeRenameResult) -> str | None:
    if result.status == "not_configured":
        return "No worktree configured; nothing to move."
    if result.status == "skipped_manual":
        return "Skipped worktree move: path is manually assigned."
    if result.status == "shared_reference":
        return (
            f"Skipped worktree move at {result.metadata.path}: "
            f"still referenced by work item(s): {result.error}"
        )
    if result.status == "missing":
        return f"Worktree directory missing at {result.metadata.path}; metadata unchanged."
    if result.status == "renamed":
        return f"🌳 Moved worktree to {result.metadata.path} ({result.metadata.branch})"
    if result.status == "failed":
        return f"Warning: could not move worktree: {result.error}"
    return None
