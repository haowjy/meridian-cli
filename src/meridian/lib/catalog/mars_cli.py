"""How Meridian calls Mars: which binary, and what it adds to a lookup or a sync.

Mars chooses the root: Meridian runs it in the command's directory and passes no ``--root`` of
its own. Mars uses the nearest project, else the user level for a lookup; a write with no
project fails with Mars's own message. Every ``--json`` object Mars prints says which root it
used (``MarsRoot``).
"""

from __future__ import annotations

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
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home

# First Mars release with a user level: Mars chooses the root, takes ``--user`` and
# ``mars sync --provided-root``, and reports ``agent_not_found`` and ``agent_renamed``.
MARS_MIN_VERSION = "0.41.0"
MARS_UPGRADE_HINT = (
    f"Meridian requires mars >= {MARS_MIN_VERSION}. Upgrade Meridian (for uv tools: "
    "uv tool upgrade meridian-cli); Meridian prefers its bundled Mars over PATH."
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


def provided_sync_args() -> tuple[str, str]:
    """``mars sync``'s host layer: this build's bundle, for the targets no harness launches."""

    return ("--provided-root", f"meridian@{__version__}={provided_dir().as_posix()}")


@dataclass(frozen=True)
class LegacyUserRoot:
    """``~/.meridian/mars``, the user root of Meridian before the Mars user level.

    ``active``: its ``mars.toml`` declares an item source or it has a ``.mars-src/``, so lookups
    still read it as ``--fallback-root`` for one minor release. ``sources``: what to give
    ``meridian mars --user add`` to move it.
    """

    path: Path
    active: bool
    sources: tuple[str, ...] = ()


def legacy_user_root() -> LegacyUserRoot | None:
    """``~/.meridian/mars`` when it exists. Meridian never deletes or writes it."""

    root = get_user_home() / "mars"
    has_own_items = (root / ".mars-src").is_dir()
    try:
        text = (root / "mars.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return LegacyUserRoot(path=root, active=True) if has_own_items else None
    except OSError:
        return LegacyUserRoot(path=root, active=True)
    try:
        manifest = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Let Mars read it and report what is wrong.
        return LegacyUserRoot(path=root, active=True)
    sources = tuple(
        source
        for table in ("dependencies", "local-dependencies")
        for source in _declared_sources(manifest.get(table), base=root)
    )
    return LegacyUserRoot(path=root, active=has_own_items or bool(sources), sources=sources)


def _declared_sources(table: object, *, base: Path) -> list[str]:
    if not isinstance(table, dict):
        return []
    sources: list[str] = []
    for name, spec in cast("dict[str, object]", table).items():
        entry = cast("dict[str, object]", spec) if isinstance(spec, dict) else {}
        url = entry.get("url")
        path = entry.get("path")
        if isinstance(url, str) and url.strip():
            sources.append(url.strip())
        elif isinstance(path, str) and path.strip():
            sources.append((base / Path(path.strip()).expanduser()).resolve().as_posix())
        else:
            sources.append(name)
    return sources


@cache
def _active_legacy_user_root() -> LegacyUserRoot | None:
    """The legacy root lookups still read, warning once per command how to move it."""

    legacy = legacy_user_root()
    if legacy is None or not legacy.active:
        return None
    steps = [f"  meridian mars --user add {source}" for source in legacy.sources]
    if (legacy.path / ".mars-src").is_dir():
        steps.append(
            f"  move {(legacy.path / '.mars-src').as_posix()} into your Mars user level's "
            ".mars-src/ (its folder: meridian mars --user list --json)"
        )
    print(
        f"warning: {legacy.path.as_posix()} is Meridian's old user Mars root; this release "
        "still reads it, the next one does not. Move it to your Mars user level:\n"
        + "\n".join(steps)
        + f"\nthen delete {legacy.path.as_posix()}",
        file=sys.stderr,
    )
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
    "MARS_UPGRADE_HINT",
    "LegacyUserRoot",
    "MarsAgentNotFoundError",
    "MarsRoot",
    "legacy_user_root",
    "mars_content_args",
    "parse_mars_root",
    "provided_dir",
    "provided_sync_args",
    "resolve_mars_executable",
]
