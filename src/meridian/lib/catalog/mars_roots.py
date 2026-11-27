"""Authoritative Mars package-root resolution and user-root initialization."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

import meridian
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home


class MarsRoots(BaseModel):
    """Resolved package roots for one Meridian project context."""

    model_config = ConfigDict(frozen=True)

    primary: Path
    scope: Literal["project", "user"]
    user: Path | None = None


def user_mars_root() -> Path:

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


def mars_content_roots(roots: MarsRoots) -> tuple[Path, ...]:
    """The installed content layers Mars reads, in its lookup order."""

    return (roots.primary,) if roots.user is None else (roots.primary, roots.user)


def mars_fallback_label(roots: MarsRoots) -> Literal["active", "n/a"]:

    return "n/a" if roots.user is None else "active"


# Mars's error codes for an agent no content layer resolves. ``agent_renamed``: only the
# ``<name>__<source>`` copies of a name collision exist, and the message names them.
MARS_AGENT_NOT_FOUND_CODES = frozenset({"agent_not_found", "agent_renamed"})


class MarsAgentNotFoundError(FileNotFoundError):
    """No content layer Mars reads has the agent, or only its renamed copies."""

    def __init__(self, code: str, message: str) -> None:
        if code == "agent_not_found":
            message = f"{message}\nList the agents Mars finds: meridian spawn subagents --all"
        super().__init__(message)


def provided_dir() -> Path:
    """This build's bundle directory: what ``mars provide`` installs and Mars's provided layer."""

    return Path(meridian.__file__).resolve().parent / "mars"


def mars_content_args(roots: MarsRoots) -> tuple[str, ...]:
    """The content layers after the primary root for a by-name agent or skill lookup.

    The user root, then this build's bundle directory, which answers only a name no
    installed agent or skill claims.
    """

    fallback = () if roots.user is None else ("--fallback-root", roots.user.as_posix())
    return (*fallback, "--provided-root", provided_dir().as_posix())


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over ``PATH``."""
    return installed_executable("mars") or shutil.which("mars")


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
    "MARS_AGENT_NOT_FOUND_CODES",
    "MarsAgentNotFoundError",
    "MarsRoots",
    "ensure_resolved_mars_root",
    "ensure_user_mars_root",
    "mars_content_args",
    "mars_content_roots",
    "mars_fallback_label",
    "provided_dir",
    "resolve_mars_executable",
    "resolve_mars_roots",
    "user_mars_root",
]
