"""How Meridian calls Mars: which binary, where it runs, and what it adds to a lookup or a sync.

Mars chooses the root. Meridian runs it in the caller's working directory, so a relative path
the caller typed means what they meant, and names Meridian's project only as where Mars's
walk-up starts (``mars_location``). Mars uses the nearest project, else the user level for a
lookup; a write with no project fails with ``project_not_found``. Every ``--json`` object Mars
prints says which root it used (``MarsRoot``).
"""

from __future__ import annotations

import shlex
import shutil
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
    row = cast("dict[str, object]", error)
    if row.get("code") == "project_not_found":
        return True
    # Mars before its distinct code reports the same error as `invalid_config`.
    message = row.get("message")
    return (
        row.get("code") == "invalid_config"
        and isinstance(message, str)
        and "no mars.toml found from " in message
    )


class MarsRoot(BaseModel):
    """The root Mars used, as its ``--json`` output reports it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["project", "user"]
    path: Path
    # Only the user level reports these; a project keeps its store in ``<path>/.mars``.
    config_dir: Path | None = None
    store_dir: Path | None = None

    @property
    def store(self) -> Path:
        return self.store_dir or self.path / ".mars"


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
        return "\n".join((lead, *(f"  {step}" for step in self.steps), f"then delete {path}"))


# What a dependency entry can say that ``mars add`` reproduces, and the flag that says it.
_ADD_LIST_FLAGS = {"agents": "--agents", "skills": "--skills", "exclude": "--exclude"}
_ADD_VALUE_FLAGS = {"subpath": "--subpath", "plugin": "--plugin"}
_ADD_BOOL_FLAGS = {"only_skills": "--only-skills", "only_agents": "--only-agents"}
_ADD_KEYS = {"url", "path", "version", *_ADD_LIST_FLAGS, *_ADD_VALUE_FLAGS, *_ADD_BOOL_FLAGS}
# Sections of the old root that every root now reads from Mars's user ``config.toml``.
_CONFIG_SECTIONS = ("settings", "models", "agents", "skills")


def legacy_user_root() -> LegacyUserRoot | None:
    """``~/.meridian/mars`` when it exists. Meridian never deletes or writes it."""

    root = get_user_home() / "mars"
    own_items = root / ".mars-src"
    has_own_items = own_items.is_dir()
    own_items_step = (
        f"move {own_items.as_posix()} into your Mars user level's .mars-src/ "
        "(root.path of meridian mars --user list --json)"
    )
    try:
        manifest = tomllib.loads((root / "mars.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not has_own_items:
            return LegacyUserRoot(path=root, active=False) if root.is_dir() else None
        return LegacyUserRoot(path=root, active=True, steps=(own_items_step,))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return LegacyUserRoot(path=root, active=True, unreadable=str(exc))
    dependency_steps = [
        _dependency_step(name, spec, table=table, base=root)
        for table in ("dependencies", "local-dependencies")
        if isinstance(entries := manifest.get(table), dict)
        for name, spec in cast("dict[str, object]", entries).items()
    ]
    sections = [f"[{name}]" for name in _CONFIG_SECTIONS if name in manifest]
    steps = [*dependency_steps]
    if has_own_items:
        steps.append(own_items_step)
    if sections:
        steps.append(
            f"move {', '.join(sections)} from {(root / 'mars.toml').as_posix()} into your Mars "
            "user config.toml (in root.config_dir of meridian mars --user list --json), which "
            "every root reads"
        )
    return LegacyUserRoot(
        path=root, active=has_own_items or bool(dependency_steps), steps=tuple(steps)
    )


def _dependency_step(name: str, spec: object, *, table: str, base: Path) -> str:
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
            f"copy [{table}.{name}] from {(base / 'mars.toml').as_posix()} into your Mars user "
            "level's mars.toml (in root.path of meridian mars --user list --json), with any "
            "relative path made absolute"
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


@cache
def _active_legacy_user_root() -> LegacyUserRoot | None:
    """The legacy root lookups still read, warning once per command how to move it."""

    legacy = legacy_user_root()
    if legacy is None or not legacy.active:
        return None
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
    "mars_content_args",
    "mars_location",
    "parse_mars_root",
    "provided_dir",
    "provided_sync_args",
    "resolve_mars_executable",
]
