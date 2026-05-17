"""Pure git subprocess seam for worktree operations.

This module is intentionally dependency-free with respect to meridian state,
config, and persistence layers. It accepts plain Path inputs and issues git
subprocess calls. Callers that need meridian integration wire it in above this
seam.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

GIT_LOCAL_ENV_VARS: tuple[str, ...] = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def git_subprocess_env() -> dict[str, str]:
    """Return env for git subprocesses with repo-scoped overrides removed."""

    env = os.environ.copy()
    for key in GIT_LOCAL_ENV_VARS:
        env.pop(key, None)
    return env


class WorktreeError(RuntimeError):
    """Base domain error for git worktree operations."""


class WorktreeCreateFailed(WorktreeError):
    """Git could not create or attach the requested worktree."""


class WorktreeConflictError(WorktreeCreateFailed):
    """The requested path or branch conflicts with an existing worktree state."""


class WorktreeRemoveFailed(WorktreeError):
    """Git could not remove the requested worktree."""


class WorktreeUnpushedError(WorktreeError):
    """The worktree branch has local commits that are not safely removable."""


class WorktreeRepoResolutionError(WorktreeError):
    """Git repository discovery failed."""


class WorktreeMoveFailed(WorktreeError):
    """Git could not move or rename the worktree."""


@dataclass(frozen=True)
class WorktreeCreateResult:
    path: Path
    branch: str
    created: bool  # False if reusing existing worktree


@dataclass(frozen=True)
class WorktreeMoveResult:
    old_path: Path
    new_path: Path
    old_branch: str
    new_branch: str
    path_moved: bool
    branch_renamed: bool


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    error_type: type[WorktreeError],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            env=git_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise error_type("git is not installed.") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        raise error_type(details or f"git {' '.join(args)} failed.") from exc


def detect_git_repo(project_root: Path) -> bool:
    """Check if project_root is inside a git repo."""
    try:
        result = _run_git(
            project_root,
            ["rev-parse", "--is-inside-work-tree"],
            error_type=WorktreeRepoResolutionError,
        )
    except WorktreeRepoResolutionError:
        return False
    return result.stdout.strip() == "true"


def resolve_main_repo_root(project_root: Path) -> Path | None:
    """Resolve the main git repo root, navigating through worktrees if needed."""
    try:
        result = _run_git(
            project_root,
            ["rev-parse", "--git-common-dir"],
            error_type=WorktreeRepoResolutionError,
        )
    except WorktreeRepoResolutionError:
        return None

    git_common_dir_raw = result.stdout.strip()
    common_dir = (project_root / git_common_dir_raw).resolve()
    if common_dir.name == ".git":
        return common_dir.parent

    logger.debug(
        "worktree_ops.resolve_main_repo_root.fallback",
        common_dir=str(common_dir),
        project_root=str(project_root),
    )
    try:
        fallback = _run_git(
            project_root,
            ["rev-parse", "--show-toplevel"],
            error_type=WorktreeRepoResolutionError,
        )
    except WorktreeRepoResolutionError:
        return None
    return Path(fallback.stdout.strip()).resolve()


def resolve_worktree_path(
    repo_root: Path,
    work_slug: str,
    worktree_base: str | None = None,
) -> Path:
    """Compute the target filesystem path for a worktree."""
    if worktree_base is not None:
        base_path = Path(worktree_base)
        if not base_path.is_absolute():
            base_path = repo_root / base_path
        base = base_path
    else:
        base = repo_root.parent / f"{repo_root.name}.worktrees"
    return (base / work_slug).resolve()


def worktree_exists(path: Path) -> bool:
    """Return True if *path* is a valid git worktree."""
    return (path / ".git").is_file()


def ensure_no_unpushed_commits(worktree_path: Path) -> None:
    """Raise when the worktree branch has local commits not on its upstream."""
    if not worktree_path.exists() or not worktree_exists(worktree_path):
        return
    try:
        result = _run_git(
            worktree_path,
            ["log", "@{upstream}..HEAD", "--oneline"],
            error_type=WorktreeUnpushedError,
        )
    except WorktreeUnpushedError as exc:
        stderr = str(exc).lower()
        if "upstream" in stderr or "no such branch" in stderr:
            raise WorktreeUnpushedError("Worktree branch has unpushed commits.") from exc
        raise WorktreeUnpushedError(
            "Unable to verify whether the worktree branch is pushed."
        ) from exc
    if result.stdout.strip():
        raise WorktreeUnpushedError("Worktree branch has unpushed commits.")


def remove_worktree(repo_root: Path, worktree_path: Path, *, force: bool = False) -> None:
    """Remove a git worktree via ``git worktree remove``."""
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    _run_git(repo_root, args, error_type=WorktreeRemoveFailed)
    logger.info(
        "worktree_ops.remove_worktree.done",
        repo_root=str(repo_root),
        worktree_path=str(worktree_path),
        force=force,
    )


def move_worktree(
    repo_root: Path,
    old_path: Path,
    new_path: Path,
    old_branch: str,
    new_branch: str,
) -> WorktreeMoveResult:
    """Move a worktree path and rename its branch as one operation with rollback."""
    if not old_path.exists() or not worktree_exists(old_path):
        raise WorktreeMoveFailed(f"Path {old_path} is not a valid git worktree.")
    if new_path.exists():
        raise WorktreeMoveFailed(f"Target path {new_path} already exists.")

    _run_git(
        repo_root,
        ["worktree", "move", str(old_path), str(new_path)],
        error_type=WorktreeMoveFailed,
    )

    branch_renamed = False
    if old_branch != new_branch:
        try:
            _run_git(
                new_path,
                ["branch", "-m", old_branch, new_branch],
                error_type=WorktreeMoveFailed,
            )
            branch_renamed = True
        except WorktreeMoveFailed as exc:
            branch_error = str(exc)
            try:
                _run_git(
                    repo_root,
                    ["worktree", "move", str(new_path), str(old_path)],
                    error_type=WorktreeMoveFailed,
                )
            except WorktreeMoveFailed as rollback_exc:
                raise WorktreeMoveFailed(
                    "Branch rename failed after moving worktree: "
                    f"{branch_error}. Rollback to '{old_path}' also failed: "
                    f"{rollback_exc}. Worktree remains at '{new_path}'."
                ) from exc
            raise WorktreeMoveFailed(
                "Branch rename failed after moving worktree: "
                f"{branch_error}. Rolled back worktree path to '{old_path}'."
            ) from exc

    logger.info(
        "worktree_ops.move_worktree.done",
        repo_root=str(repo_root),
        old_path=str(old_path),
        new_path=str(new_path),
        old_branch=old_branch,
        new_branch=new_branch,
        branch_renamed=branch_renamed,
    )
    return WorktreeMoveResult(
        old_path=old_path,
        new_path=new_path,
        old_branch=old_branch,
        new_branch=new_branch,
        path_moved=old_path != new_path,
        branch_renamed=branch_renamed,
    )


def _current_worktree_branch(worktree_path: Path) -> str:
    result = _run_git(
        worktree_path,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        error_type=WorktreeCreateFailed,
    )
    return result.stdout.strip()


def current_worktree_branch(worktree_path: Path) -> str:
    """Return the currently checked-out branch for a worktree path."""
    return _current_worktree_branch(worktree_path)


def create_worktree(
    repo_root: Path,
    target_path: Path,
    branch: str,
) -> WorktreeCreateResult:
    """Create a git worktree at *target_path* on *branch*."""
    if target_path.exists():
        if not worktree_exists(target_path):
            raise WorktreeCreateFailed(
                f"Path {target_path} already exists but is not a valid git worktree."
            )

        target_main = resolve_main_repo_root(target_path)
        expected_main = resolve_main_repo_root(repo_root)
        if target_main is None or expected_main is None or target_main != expected_main:
            raise WorktreeConflictError(
                f"Path {target_path} is a git worktree for a different repository."
            )

        current_branch = _current_worktree_branch(target_path)
        if current_branch != branch:
            raise WorktreeConflictError(
                f"Worktree at {target_path} is on branch '{current_branch}', not '{branch}'."
            )
        logger.info(
            "worktree_ops.create_worktree.reuse",
            path=str(target_path),
            branch=branch,
        )
        return WorktreeCreateResult(path=target_path, branch=branch, created=False)

    try:
        _run_git(
            repo_root,
            ["worktree", "add", str(target_path), "-b", branch],
            error_type=WorktreeCreateFailed,
        )
        logger.info(
            "worktree_ops.create_worktree.created",
            path=str(target_path),
            branch=branch,
        )
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)
    except WorktreeCreateFailed as exc:
        if "already exists" not in str(exc):
            raise

    try:
        _run_git(
            repo_root,
            ["worktree", "add", str(target_path), branch],
            error_type=WorktreeCreateFailed,
        )
        logger.info(
            "worktree_ops.create_worktree.created_existing_branch",
            path=str(target_path),
            branch=branch,
        )
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)
    except WorktreeCreateFailed as exc:
        if "is already checked out" in str(exc):
            raise WorktreeConflictError(
                f"Branch '{branch}' is already checked out in another worktree."
            ) from exc
        raise


__all__ = [
    "WorktreeConflictError",
    "WorktreeCreateFailed",
    "WorktreeCreateResult",
    "WorktreeError",
    "WorktreeMoveFailed",
    "WorktreeMoveResult",
    "WorktreeRemoveFailed",
    "WorktreeRepoResolutionError",
    "WorktreeUnpushedError",
    "create_worktree",
    "current_worktree_branch",
    "detect_git_repo",
    "ensure_no_unpushed_commits",
    "move_worktree",
    "remove_worktree",
    "resolve_main_repo_root",
    "resolve_worktree_path",
    "worktree_exists",
]
