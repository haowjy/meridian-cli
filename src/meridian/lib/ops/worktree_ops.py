"""Pure git subprocess seam for worktree operations.

This module is intentionally dependency-free with respect to meridian state,
config, and persistence layers. It accepts plain Path inputs and issues git
subprocess calls. Callers that need meridian integration wire it in above this
seam.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WorktreeCreateResult:
    path: Path
    branch: str
    created: bool  # False if reusing existing worktree


def detect_git_repo(project_root: Path) -> bool:
    """Check if project_root is inside a git repo.

    Uses ``git rev-parse --is-inside-work-tree``.
    Returns False if git is not installed or the path is not in a repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resolve_main_repo_root(project_root: Path) -> Path | None:
    """Resolve the main git repo root, navigating through worktrees if needed.

    Uses ``git rev-parse --git-common-dir`` to find the shared ``.git``
    directory.  From a worktree that path is absolute; from the main repo it is
    the relative string ``.git``.  In both cases the main repo root is the
    parent of the resolved common-dir path.

    Falls back to ``git rev-parse --show-toplevel`` when the common-dir path
    does not end in ``.git`` (unusual git configurations with relocated git
    dirs).  Returns None if the path is not inside a git repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        git_common_dir_raw = result.stdout.strip()
        # Resolve to an absolute path.  The output may be relative (e.g. ".git"
        # when run from the main repo) so anchor it to project_root.
        common_dir = (project_root / git_common_dir_raw).resolve()
        if common_dir.name == ".git":
            return common_dir.parent

        # Fallback for unusual git configurations where the common dir is not
        # named ".git" (bare repos with relocated git dirs, etc.).
        logger.debug(
            "worktree_ops.resolve_main_repo_root.fallback",
            common_dir=str(common_dir),
            project_root=str(project_root),
        )
        fallback = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(fallback.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_worktree_path(
    repo_root: Path,
    work_slug: str,
    worktree_base: str | None = None,
) -> Path:
    """Compute the target filesystem path for a worktree.

    Default: ``<repo_parent>/<repo_name>.worktrees/<work_slug>/``
    If *worktree_base* is given, it is used as the parent directory instead.
    All returned paths are fully resolved (no symlinks, no ``..`` components).
    """
    if worktree_base is not None:
        base = Path(worktree_base)
    else:
        base = repo_root.parent / f"{repo_root.name}.worktrees"
    return (base / work_slug).resolve()


def worktree_exists(path: Path) -> bool:
    """Return True if *path* is a valid git worktree.

    A valid worktree has a ``.git`` *file* (not a directory) that points back
    to the main repo's worktree metadata directory.
    """
    git_entry = path / ".git"
    return git_entry.is_file()


def is_worktree_dirty(worktree_path: Path) -> bool:
    """Return True if the worktree at *worktree_path* has uncommitted changes.

    Uses ``git -C <path> status --porcelain``.  Returns False if the path does
    not exist or is not a valid worktree rather than raising.
    """
    if not worktree_path.exists() or not worktree_exists(worktree_path):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def create_worktree(
    repo_root: Path,
    target_path: Path,
    branch: str,
) -> WorktreeCreateResult:
    """Create a git worktree at *target_path* on *branch*.

    Behavior:
    1. If *target_path* already exists and is a valid worktree, reuse it
       (``created=False``).
    2. If *target_path* exists but is NOT a valid worktree, raise ValueError.
    3. Try ``git worktree add <target> -b <branch>`` (create new branch).
    4. If that fails because the branch already exists, try
       ``git worktree add <target> <branch>`` (attach to existing branch).
    5. If the branch is already checked out in another worktree, raise
       ValueError with a clear message.
    6. All other git errors propagate as subprocess.CalledProcessError.
    """
    if target_path.exists():
        if worktree_exists(target_path):
            logger.info(
                "worktree_ops.create_worktree.reuse",
                path=str(target_path),
                branch=branch,
            )
            return WorktreeCreateResult(path=target_path, branch=branch, created=False)
        raise ValueError(
            f"Path {target_path} already exists but is not a valid git worktree. "
            "Remove or relocate it before creating a worktree here."
        )

    # Attempt 1: create a new branch and check it out in the new worktree.
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                str(target_path),
                "-b",
                branch,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info(
            "worktree_ops.create_worktree.created",
            path=str(target_path),
            branch=branch,
        )
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        # Git emits "already exists" or "A branch named '…' already exists"
        # when the branch was previously created.  Fall through to attempt 2.
        if "already exists" not in stderr:
            raise

    # Attempt 2: attach to the existing branch.
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                str(target_path),
                branch,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info(
            "worktree_ops.create_worktree.created_existing_branch",
            path=str(target_path),
            branch=branch,
        )
        return WorktreeCreateResult(path=target_path, branch=branch, created=True)
    except subprocess.CalledProcessError as exc2:
        stderr2 = exc2.stderr or ""
        if "is already checked out" in stderr2:
            raise ValueError(
                f"Branch '{branch}' is already checked out in another worktree. "
                "Use a different branch name or remove the conflicting worktree first."
            ) from exc2
        raise
