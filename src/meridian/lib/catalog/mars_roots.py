"""Authoritative Mars package-root resolution and user-root initialization."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.user_paths import get_user_home


class MarsRoots(BaseModel):
    """Resolved package roots for one Meridian project context."""

    model_config = ConfigDict(frozen=True)

    primary: Path
    scope: Literal["project", "user"]
    user: Path | None = None


def user_mars_root() -> Path:
    """Return the user-scoped Mars project root."""

    return (get_user_home() / "mars").resolve()


def resolve_mars_roots(project_root: Path) -> MarsRoots:
    """Resolve package content using Mars's nearest-``mars.toml`` walk-up."""

    resolved_root = project_root.expanduser().resolve()
    resolved_user_root = user_mars_root()

    # The user root is always user scope, even after it has been initialized.
    # This keeps authoring safeguards stable when invoked from inside that root.
    if resolved_root == resolved_user_root:
        return MarsRoots(primary=resolved_user_root, scope="user")

    for candidate in (resolved_root, *resolved_root.parents):
        if (candidate / "mars.toml").is_file():
            if candidate == resolved_user_root:
                return MarsRoots(primary=resolved_user_root, scope="user")
            return MarsRoots(
                primary=candidate,
                scope="project",
                user=resolved_user_root,
            )

    return MarsRoots(primary=resolved_user_root, scope="user")


def resolve_mars_fallback_root(roots: MarsRoots) -> Path | None:
    """Return the lower-precedence content root for future Mars fallback flags.

    Meridian uses this today for its own agent and skill discovery. Once Mars
    supports layered content, this is the root to pass as ``--fallback-root``
    to ``launch-bundle`` and ``agents show/list``.
    """

    return roots.user


def mars_content_roots(roots: MarsRoots) -> tuple[Path, ...]:
    """Return package-content roots in highest-to-lowest precedence order."""

    fallback = resolve_mars_fallback_root(roots)
    return (roots.primary,) if fallback is None else (roots.primary, fallback)


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over ``PATH``."""

    # Keep the wrapper path intact: uv tool scripts point at a symlinked Python
    # binary, and resolving it jumps out of the tool environment where sibling
    # scripts (including ``mars``) live.
    scripts_dir = Path(sys.executable).parent
    for name in ("mars", "mars.exe"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mars")


def ensure_user_mars_root(
    *,
    executable: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> Path:
    """Initialize the user Mars project once, under a Meridian-owned lock."""

    root = user_mars_root()
    marker = root / "mars.toml"
    if marker.is_file():
        return root

    lock_path = get_user_home() / "locks" / "user-mars-root.flock"
    with lock_file(lock_path):
        if marker.is_file():
            return root

        mars = executable or resolve_mars_executable()
        if mars is None:
            raise RuntimeError(
                "Mars binary not found. Mars is required to initialize user packages. "
                "Run 'meridian doctor' to diagnose."
            )
        try:
            run_command = run or subprocess.run
            result = run_command(
                [mars, "init", "--root", root.as_posix()],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Failed to initialize the user Mars root.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Failed to initialize the user Mars root{suffix}")
        if not marker.is_file():
            raise RuntimeError("Mars initialized without creating the user mars.toml.")
    return root


def ensure_resolved_mars_root(
    roots: MarsRoots,
    *,
    executable: str | None = None,
) -> None:
    """Ensure a resolved user primary exists before invoking Mars against it."""

    if roots.scope == "user":
        ensure_user_mars_root(executable=executable)


__all__ = [
    "MarsRoots",
    "ensure_resolved_mars_root",
    "ensure_user_mars_root",
    "mars_content_roots",
    "resolve_mars_executable",
    "resolve_mars_fallback_root",
    "resolve_mars_roots",
    "user_mars_root",
]
