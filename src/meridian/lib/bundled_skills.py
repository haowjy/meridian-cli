"""Provide and read Meridian's packaged operational skills."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import cast

import meridian
from meridian import __version__
from meridian.lib.catalog.agent import AgentMode
from meridian.lib.core.domain import SkillContent


@dataclass(frozen=True)
class BundledSkill:
    """Metadata for one skill shipped inside the Meridian wheel."""

    name: str
    description: str
    mode: AgentMode | None = None


@dataclass(frozen=True)
class BundledSkillSource:
    """Locations this command may read for Meridian's bundled skills."""

    skills_dir: Path | None
    provided: frozenset[str]
    sync_pending: bool = False


WHEEL = BundledSkillSource(skills_dir=None, provided=frozenset())
SYNC_PENDING_MESSAGE = "an interrupted mars sync is pending; run `meridian mars sync`"


def _parse_skill_metadata(text: str, *, folder_name: str) -> BundledSkill:
    import frontmatter  # pyright: ignore[reportMissingTypeStubs]  # No stubs upstream.

    post = frontmatter.loads(text)
    metadata = dict(post.metadata)
    name = str(metadata.get("name", "")).strip()
    description = str(metadata.get("description", "")).strip()
    raw_mode = metadata.get("mode")
    if not name or not description:
        raise RuntimeError(f"bundled skill {folder_name!r} needs name and description")
    if name != folder_name:
        raise RuntimeError(
            f"bundled skill folder {folder_name!r} does not match frontmatter name {name!r}"
        )
    mode: AgentMode | None = None
    if raw_mode == "primary":
        mode = "primary"
    elif raw_mode == "subagent":
        mode = "subagent"
    elif raw_mode is not None:
        raise RuntimeError(
            f"bundled skill {folder_name!r} mode must be 'primary' or 'subagent'"
        )
    return BundledSkill(name=name, description=description, mode=mode)


def _skills_root() -> Traversable:
    return files("meridian.skills")


def _package_dir() -> Path:
    return Path(meridian.__file__).resolve().parent


@lru_cache(maxsize=1)
def list_bundled_skills() -> tuple[BundledSkill, ...]:
    """Return packaged skills in stable name order."""

    skills: list[BundledSkill] = []
    for skill_dir in sorted(_skills_root().iterdir(), key=lambda entry: entry.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith(("_", ".")):
            continue
        skill_file = skill_dir.joinpath("SKILL.md")
        if not skill_file.is_file():
            raise RuntimeError(
                f"bundled skill {skill_dir.name!r} is missing SKILL.md"
            )
        skills.append(
            _parse_skill_metadata(
                skill_file.read_text(encoding="utf-8"),
                folder_name=skill_dir.name,
            )
        )
    return tuple(skills)


def provide_bundled_skills(
    primary_root: Path,
    *,
    executable: str | None,
) -> BundledSkillSource:
    """Ask Mars to install this build's bundled skills into ``primary_root``."""

    if executable is None:
        return WHEEL
    command = [
        executable,
        "provide",
        f"meridian@{__version__}={_package_dir()}",
        "--root",
        primary_root.as_posix(),
        "--json",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return WHEEL
    if result.returncode != 0:
        return WHEEL

    try:
        payload_obj = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return WHEEL
    if not isinstance(payload_obj, dict):
        return WHEEL
    payload = cast("dict[str, object]", payload_obj)
    status = payload.get("status")
    if status == "sync-pending":
        return BundledSkillSource(
            skills_dir=None,
            provided=frozenset(),
            sync_pending=True,
        )
    if status not in {"up-to-date", "applied"}:
        return WHEEL

    raw_skills = payload.get("skills")
    if not isinstance(raw_skills, list):
        return WHEEL
    known_names = {skill.name for skill in list_bundled_skills()}
    provided = frozenset(
        item
        for item in cast("list[object]", raw_skills)
        if isinstance(item, str) and item in known_names
    )
    if status == "applied":
        raw_warnings = payload.get("warnings")
        if isinstance(raw_warnings, list):
            for warning in cast("list[object]", raw_warnings):
                if isinstance(warning, str) and warning.strip():
                    print(warning, file=sys.stderr)
    return BundledSkillSource(
        skills_dir=primary_root / ".mars" / "skills",
        provided=provided,
    )


def _skill_dir(name: str, source: BundledSkillSource) -> Traversable:
    normalized = name.strip()
    if normalized != name or not normalized:
        raise ValueError(f"unknown Meridian skill: {name}")
    known_names = {skill.name for skill in list_bundled_skills()}
    if normalized not in known_names:
        available = ", ".join(sorted(known_names))
        raise ValueError(f"unknown Meridian skill {name!r}; choose one of: {available}")
    if normalized in source.provided:
        assert source.skills_dir is not None
        return source.skills_dir / normalized
    return _skills_root().joinpath(normalized)


def _resource_parts(resource_path: str) -> tuple[str, ...]:
    if "\\" in resource_path:
        raise ValueError("resource path must use '/' and stay inside the skill folder")
    path = PurePosixPath(resource_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be relative and stay inside the skill folder")
    return path.parts


def load_bundled_skill(name: str, source: BundledSkillSource) -> SkillContent:
    """Load a skill's body and the real path of its ``SKILL.md``."""

    import frontmatter  # pyright: ignore[reportMissingTypeStubs]  # No stubs upstream.

    skill_file = _skill_dir(name, source).joinpath("SKILL.md")
    post = frontmatter.loads(skill_file.read_text(encoding="utf-8"))
    return SkillContent(
        name=name,
        description=str(dict(post.metadata)["description"]).strip(),
        content=post.content,
        path=str(skill_file),
    )


def read_bundled_skill_resource(
    name: str,
    resource_path: str,
    source: BundledSkillSource,
) -> str:
    """Read one file beneath a skill's folder."""

    target = _skill_dir(name, source).joinpath(*_resource_parts(resource_path))
    if not target.is_file():
        raise ValueError(f"resource not found for Meridian skill {name!r}: {resource_path}")
    return target.read_text(encoding="utf-8")


__all__ = [
    "SYNC_PENDING_MESSAGE",
    "WHEEL",
    "BundledSkill",
    "BundledSkillSource",
    "list_bundled_skills",
    "load_bundled_skill",
    "provide_bundled_skills",
    "read_bundled_skill_resource",
]

