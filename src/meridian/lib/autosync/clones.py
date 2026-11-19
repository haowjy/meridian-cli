"""The inside-a-context-clone test, shared by every refusal and the primary-launch warning."""

from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.state.user_paths import get_user_home


def enclosing_clone(path: Path) -> Path | None:
    """Return the clone under ``<home>/git/`` that holds ``path``, or ``None``.

    A clone is a directory directly under the realpath of ``<home>/git/`` with a ``.git``
    directory. Pure path operations and one ``stat``: no git runs, so a broken repository
    still counts. Linked worktrees sit beside the clone (``<slug>.worktrees/``) and have
    no ``.git`` directory there, so they pass.
    """

    git_root = Path(os.path.realpath(get_user_home() / "git"))
    target = Path(os.path.realpath(path))
    try:
        relative = target.relative_to(git_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    clone = git_root / relative.parts[0]
    return clone if (clone / ".git").is_dir() else None


def clone_refusal_message(task_dir: Path, clone: Path) -> str:
    return (
        f"task dir `{task_dir}` is inside the context clone `{clone}`, which other agents "
        "and autosync share, so git commands there act on that clone. For branch work use "
        f"a linked worktree: `git -C {clone} worktree add {clone}.worktrees/<name> -b "
        "<branch>`. Put fixtures outside the clone."
    )


def project_in_clone_message(clone: Path) -> str:
    return (
        f"cannot create a Meridian project inside the context clone `{clone}`, which other "
        "agents and autosync share. Run `meridian init` in your project instead."
    )


def clone_launch_warning(task_dir: Path, clone: Path) -> str:
    return (
        f"`{task_dir}` is inside the context clone `{clone}`; "
        "spawns from this session will be refused"
    )


def require_outside_clone(task_dir: Path) -> None:
    clone = enclosing_clone(task_dir)
    if clone is not None:
        raise ValueError(clone_refusal_message(task_dir, clone))
