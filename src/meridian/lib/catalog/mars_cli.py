"""How Meridian calls Mars: which binary, where it runs, and what it adds to a lookup or a sync.

Mars chooses the root. Meridian runs it in the caller's working directory, so a relative path
the caller typed means what they meant, and names Meridian's project only as where Mars's
walk-up starts (``mars_location``). Mars uses the nearest project, else the user level for a
lookup; a write with no project fails with ``project_not_found``. Every ``--json`` object Mars
prints says which root it used (``MarsRoot``).
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tomllib
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

# First Mars release with a user level: Mars chooses the root, takes ``--user`` and
# ``mars sync --provided-root``, and reports ``agent_not_found`` and ``agent_renamed``.
MARS_MIN_VERSION = "0.41.0"
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
MARS_SYNCING_COMMANDS = frozenset(
    {"sync", "add", "remove", "upgrade", "repair", "rename", "override", "adopt", "link"}
)


def provided_sync_args() -> tuple[str, str]:
    """A project sync's host layer: this build's bundle, for the targets no harness launches."""

    return ("--provided-root", f"meridian@{__version__}={provided_dir().as_posix()}")


@dataclass(frozen=True)
class LegacyUserRoot:
    """``~/.meridian/mars``, the user root of Meridian before the Mars user level.

    ``active``: it holds items (a dependency or a ``.mars-src/``), or its ``mars.toml`` cannot be
    read, so lookups still pass it as ``--fallback-root`` for one minor release and let Mars
    report what is wrong. ``steps``: how to move each thing it holds to the Mars user level.
    """

    path: Path
    active: bool
    steps: tuple[str, ...] = ()
    # Why its ``mars.toml`` cannot be read; None when it can or there is none.
    unreadable: str | None = None

    def migration_message(self) -> str:
        path = self.path.as_posix()
        if self.unreadable is not None:
            return (
                f"{path}/mars.toml, Meridian's old user Mars root, cannot be read "
                f"({self.unreadable}). Fix it or move what it held to your Mars user level "
                f"(meridian mars --user add <source>), then delete {path}."
            )
        if not self.steps:
            return f"{path} holds nothing Meridian or Mars reads; delete it."
        lead = (
            f"{path} is Meridian's old user Mars root; this release still reads it, the next "
            "one does not. Move it to your Mars user level:"
            if self.active
            else f"{path} holds no items and Meridian no longer reads it. Before deleting it:"
        )
        delete = (
            f"then delete {path}, once you no longer need to roll back to Meridian 0.28, "
            "which reads only that folder"
        )
        return "\n".join((lead, *(f"  {step}" for step in self.steps), delete))


# What a dependency entry can say that ``mars add`` reproduces, and the flag that says it.
_ADD_LIST_FLAGS = {"agents": "--agents", "skills": "--skills", "exclude": "--exclude"}
_ADD_VALUE_FLAGS = {"subpath": "--subpath", "plugin": "--plugin"}
_ADD_BOOL_FLAGS = {"only_skills": "--only-skills", "only_agents": "--only-agents"}
_ADD_KEYS = {"url", "path", "version", *_ADD_LIST_FLAGS, *_ADD_VALUE_FLAGS, *_ADD_BOOL_FLAGS}
# Sections of the old root that every root now reads from Mars's user ``config.toml``.
_CONFIG_SECTIONS = ("settings", "models", "agents", "skills")


@cache
def legacy_user_root() -> LegacyUserRoot | None:
    """``~/.meridian/mars`` when it exists. Meridian never deletes or writes it.

    Its move steps name the Mars user level's real paths, or, when Mars can't say where that
    is, which field of ``meridian mars --user list --json`` holds each path.
    """

    root = get_user_home() / "mars"
    own_items = root / ".mars-src"
    has_own_items = own_items.is_dir()
    try:
        manifest: dict[str, object] | None = tomllib.loads(
            (root / "mars.toml").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        if not has_own_items and not root.is_dir():
            return None
        manifest = None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return LegacyUserRoot(path=root, active=True, unreadable=str(exc))
    manifest = manifest or {}
    tables = [
        (table, cast("dict[str, object]", entries))
        for table in ("dependencies", "local-dependencies")
        if isinstance(entries := manifest.get(table), dict) and entries
    ]
    sections = [f"[{name}]" for name in _CONFIG_SECTIONS if name in manifest]
    if not (tables or has_own_items or sections):
        return LegacyUserRoot(path=root, active=False)

    user_level = mars_user_level()
    if user_level is not None:
        user_items = f"{(user_level.path / '.mars-src').as_posix()}/"
        user_manifest = (user_level.path / "mars.toml").as_posix()
        user_config = (user_level.path / "config.toml").as_posix()
    else:
        user_items = (
            "your Mars user level's .mars-src/ (root.path of meridian mars --user list --json)"
        )
        user_manifest = (
            "your Mars user level's mars.toml (in root.path of meridian mars --user list --json)"
        )
        user_config = (
            "your Mars user config.toml (in root.path of meridian mars --user list --json)"
        )
    steps = [
        _dependency_step(name, spec, table=table, base=root, user_manifest=user_manifest)
        for table, entries in tables
        for name, spec in entries.items()
    ]
    if has_own_items:
        steps.append(f"move {own_items.as_posix()} into {user_items}")
    if sections:
        steps.append(
            f"move {', '.join(sections)} from {(root / 'mars.toml').as_posix()} into "
            f"{user_config}, which every root reads"
        )
    if has_own_items:
        # `add` installs what it adds; moved `.mars-src/` items install on the next sync.
        steps.append("meridian mars --user sync")
    return LegacyUserRoot(path=root, active=has_own_items or bool(tables), steps=tuple(steps))


def mars_user_level() -> MarsRoot | None:
    """The Mars user level's root, as ``mars --user list --json`` reports it; None if Mars can't."""

    executable = resolve_mars_executable()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--user", "--json", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload: object = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return parse_mars_root(payload)


def _dependency_step(name: str, spec: object, *, table: str, base: Path, user_manifest: str) -> str:
    """The ``meridian mars --user add`` that recreates one dependency, options included.

    An entry with a key ``mars add`` cannot set (``rename``, say) is copied instead.
    """

    entry = cast("dict[str, object]", spec) if isinstance(spec, dict) else {}
    url = entry.get("url")
    path = entry.get("path")
    if isinstance(url, str) and url.strip():
        source = url.strip()
    elif isinstance(path, str) and path.strip():
        source = (base / Path(path.strip()).expanduser()).resolve().as_posix()
    else:
        source = None
    if source is None or not set(entry) <= _ADD_KEYS:
        return (
            f"copy [{table}.{name}] from {(base / 'mars.toml').as_posix()} into "
            f"{user_manifest}, with any relative path made absolute"
        )
    version = entry.get("version")
    if isinstance(url, str) and isinstance(version, str) and version.strip():
        source = f"{source}#{version.strip()}"
    words = ["meridian mars --user add", shlex.quote(source)]
    for key, flag in _ADD_VALUE_FLAGS.items():
        if isinstance(value := entry.get(key), str):
            words += [flag, shlex.quote(value)]
    for key, flag in _ADD_LIST_FLAGS.items():
        if isinstance(value := entry.get(key), list):
            items = [str(item) for item in cast("list[object]", value)]
            words += [flag, shlex.quote(",".join(items))]
    words += [flag for key, flag in _ADD_BOOL_FLAGS.items() if entry.get(key) is True]
    return " ".join(words)


# Set once this run has told the user how to move ``~/.meridian/mars``.
_legacy_user_root_told = False


def mark_legacy_user_root_reported() -> None:
    """A command that prints the move steps itself (``doctor``): lookups in this run don't."""

    global _legacy_user_root_told
    _legacy_user_root_told = True


def _active_legacy_user_root() -> LegacyUserRoot | None:
    """The legacy root lookups still read, warning once per run how to move it."""

    global _legacy_user_root_told
    legacy = legacy_user_root()
    if legacy is None or not legacy.active:
        return None
    if not _legacy_user_root_told:
        _legacy_user_root_told = True
        print(f"warning: {legacy.migration_message()}", file=sys.stderr)
    return legacy


def mars_content_args() -> tuple[str, ...]:
    """The content layers after the root Mars chose, for a by-name agent or skill lookup.

    The legacy ``~/.meridian/mars`` while it still holds items, then this build's bundle
    directory, which answers only a name no installed agent or skill claims.
    """

    legacy = _active_legacy_user_root()
    fallback = () if legacy is None else ("--fallback-root", legacy.path.as_posix())
    return (*fallback, "--provided-root", provided_dir().as_posix())


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over ``PATH``."""
    return installed_executable("mars") or shutil.which("mars")


__all__ = [
    "MARS_AGENT_NOT_FOUND_CODES",
    "MARS_MIN_VERSION",
    "MARS_SYNCING_COMMANDS",
    "MARS_UPGRADE_HINT",
    "LegacyUserRoot",
    "MarsAgentNotFoundError",
    "MarsLocation",
    "MarsRoot",
    "is_project_not_found",
    "legacy_user_root",
    "mark_legacy_user_root_reported",
    "mars_content_args",
    "mars_location",
    "mars_user_level",
    "parse_mars_root",
    "provided_dir",
    "provided_sync_args",
    "resolve_mars_executable",
]
