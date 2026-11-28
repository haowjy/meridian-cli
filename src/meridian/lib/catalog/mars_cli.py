"""How Meridian calls Mars: which binary, where it runs, and what it adds to a lookup or a sync.

Mars chooses the root. Meridian runs it in the caller's working directory, so a relative path
the caller typed means what they meant, and names Meridian's project only as where Mars's
walk-up starts (``mars_location``). Mars uses the nearest project, else the user level for a
lookup; a write with no project fails with ``project_not_found``. Every ``--json`` object Mars
prints says which root it used (``MarsRoot``).
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

import meridian
from meridian import __version__
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.platform import get_home_path
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home

# 0.41.0 added the user level: Mars chooses the root, takes ``--user`` and
# ``mars sync --provided-root``, and reports ``agent_not_found`` and ``agent_renamed``.
# 0.41.1 is the first whose ``validate`` takes ``--provided-root``.
MARS_MIN_VERSION = "0.41.1"
MARS_UPGRADE_HINT = (
    f"Meridian requires mars >= {MARS_MIN_VERSION}. Upgrade Meridian (for uv tools: "
    "uv tool upgrade meridian-cli); Meridian prefers its bundled Mars over PATH."
)


@dataclass(frozen=True)
class MarsLocation:
    """Where a Mars command runs, and the ``--root`` that starts its walk-up, if any."""

    cwd: Path
    root_args: tuple[str, ...] = ()

    def command(self, executable: str, *args: str) -> list[str]:
        return [executable, *self.root_args, *args]


def mars_location(project_root: Path | None = None) -> MarsLocation:
    """Run Mars in the working directory; start its walk-up at Meridian's project when named.

    A project named by the caller or ``MERIDIAN_PROJECT_DIR`` that the working directory is not
    inside (an agent in a worktree) becomes ``--root``, which Mars treats only as the start of
    its walk-up and then chooses the root. Home is never a project, so a project named as
    exactly home names none and Mars walks up from the working directory. A relative path Mars
    reads resolves from the working directory when it exists there.
    """

    resolution = resolve_project_root_resolution(project_root)
    cwd = resolution.execution_cwd
    named = resolution.project_root
    if (
        resolution.source == "cwd"
        or cwd.is_relative_to(named)
        or named == get_home_path().expanduser().resolve()
    ):
        return MarsLocation(cwd=cwd)
    return MarsLocation(cwd=cwd, root_args=("--root", resolution.project_root.as_posix()))


def is_project_not_found(payload: object) -> bool:
    """Whether a Mars ``--json`` error says no project encloses where it looked."""

    if not isinstance(payload, dict):
        return False
    error = cast("dict[str, object]", payload).get("error")
    if not isinstance(error, dict):
        return False
    return cast("dict[str, object]", error).get("code") == "project_not_found"


class MarsRoot(BaseModel):
    """The root Mars used, as its ``--json`` output reports it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["project", "user"]
    path: Path

    @property
    def store(self) -> Path:
        # The user level is its own store; a project keeps its store in ``<path>/.mars``.
        return self.path if self.kind == "user" else self.path / ".mars"


def parse_mars_root(payload: object) -> MarsRoot | None:
    """The ``root`` of one Mars ``--json`` object, or None when it has none."""

    if not isinstance(payload, dict):
        return None
    try:
        return MarsRoot.model_validate(cast("dict[str, object]", payload).get("root"))
    except ValidationError:
        return None


# Mars's error codes for an agent no content layer resolves. ``agent_renamed``: only the
# ``<name>__<source>`` copies of a name collision exist, and the message names them.
MARS_AGENT_NOT_FOUND_CODES = frozenset({"agent_not_found", "agent_renamed"})


class MarsAgentNotFoundError(FileNotFoundError):
    """No content layer Mars reads has the agent, or only its renamed copies."""

    def __init__(self, code: str, message: str) -> None:
        if code == "agent_not_found":
            message = (
                f"{message}\nList the agents Mars finds: meridian spawn subagents --all\n"
                "Install one for every project: meridian mars --user add <source>"
            )
        super().__init__(message)


def provided_dir() -> Path:
    """This build's bundle directory: Meridian's own agents and skills, read in place."""

    return Path(meridian.__file__).resolve().parent / "mars"


# The Mars commands that run a project sync, each taking the host layer
# (``--provided-root HOST@VERSION=DIR``), as Mars's ``docs/cli/commands.md`` lists them.
# ``validate`` is a sync that writes nothing.
MARS_SYNCING_COMMANDS = frozenset(
    {
        "sync",
        "add",
        "remove",
        "upgrade",
        "repair",
        "rename",
        "override",
        "adopt",
        "link",
        "validate",
    }
)


def provided_sync_args() -> tuple[str, str]:
    """A project sync's host layer: this build's bundle, for the targets no harness launches."""

    return ("--provided-root", f"meridian@{__version__}={provided_dir().as_posix()}")


_MIGRATE_SCRIPT_URL = (
    "https://raw.githubusercontent.com/haowjy/meridian-cli/main/scripts/migrate-to-dot-mars.py"
)
_UPGRADING_URL = (
    "https://github.com/haowjy/meridian-cli/blob/main/docs/upgrading.md"
    "#mars-chooses-the-root-meridianmars-retires"
)


@cache
def legacy_user_root() -> Path | None:
    """``~/.meridian/mars``, Meridian's user root before the Mars user level, when it exists.

    Lookups still pass it as ``--fallback-root`` for one minor release. Mars ignores a fallback
    with no ``mars.toml`` and warns about a lock it cannot load, so Meridian only checks that the
    folder exists; it never opens, writes or deletes it.
    """

    root = get_user_home() / "mars"
    return root if root.is_dir() else None


def legacy_user_root_message(root: Path) -> str:
    return (
        f"{root.as_posix()} is Meridian's old user Mars root; this release still reads it, the "
        "next minor release does not. Move it to your Mars user level with the migration "
        f"script: uv run {_MIGRATE_SCRIPT_URL} shows what it would move; add --apply to move "
        f"it. See {_UPGRADING_URL}"
    )


# Set once this run has warned about ``~/.meridian/mars``.
_legacy_user_root_told = False


def mark_legacy_user_root_reported() -> None:
    """A command that reports the legacy root itself (``doctor``): lookups in this run don't."""

    global _legacy_user_root_told
    _legacy_user_root_told = True


def mars_content_args() -> tuple[str, ...]:
    """The content layers after the root Mars chose, for a by-name agent or skill lookup.

    The legacy ``~/.meridian/mars`` while it exists, warning once per run, then this build's bundle
    directory, which answers only a name no installed agent or skill claims.
    """

    global _legacy_user_root_told
    legacy = legacy_user_root()
    if legacy is not None and not _legacy_user_root_told:
        _legacy_user_root_told = True
        print(f"warning: {legacy_user_root_message(legacy)}", file=sys.stderr)
    fallback = () if legacy is None else ("--fallback-root", legacy.as_posix())
    return (*fallback, "--provided-root", provided_dir().as_posix())


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over ``PATH``."""
    return installed_executable("mars") or shutil.which("mars")


__all__ = [
    "MARS_AGENT_NOT_FOUND_CODES",
    "MARS_MIN_VERSION",
    "MARS_SYNCING_COMMANDS",
    "MARS_UPGRADE_HINT",
    "MarsAgentNotFoundError",
    "MarsLocation",
    "MarsRoot",
    "is_project_not_found",
    "legacy_user_root",
    "legacy_user_root_message",
    "mark_legacy_user_root_reported",
    "mars_content_args",
    "mars_location",
    "parse_mars_root",
    "provided_dir",
    "provided_sync_args",
    "resolve_mars_executable",
]
