"""Read-only access to Meridian's packaged operational skills."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath


@dataclass(frozen=True)
class BundledSkill:
    """Metadata for one skill shipped inside the Meridian wheel."""

    name: str
    description: str
    spawn_only: bool = False


def _parse_skill_metadata(text: str, *, folder_name: str) -> BundledSkill:
    import frontmatter  # pyright: ignore[reportMissingTypeStubs]  # No stubs upstream.

    post = frontmatter.loads(text)
    metadata = dict(post.metadata)
    name = str(metadata.get("name", "")).strip()
    description = str(metadata.get("description", "")).strip()
    spawn_only = metadata.get("spawn-only", False)
    if not name or not description:
        raise RuntimeError(f"bundled skill {folder_name!r} needs name and description")
    if name != folder_name:
        raise RuntimeError(
            f"bundled skill folder {folder_name!r} does not match frontmatter name {name!r}"
        )
    if not isinstance(spawn_only, bool):
        raise RuntimeError(f"bundled skill {folder_name!r} spawn-only must be a boolean")
    return BundledSkill(name=name, description=description, spawn_only=spawn_only)


def _skills_root() -> Traversable:
    return files("meridian.skills")


@lru_cache(maxsize=1)
def list_bundled_skills() -> tuple[BundledSkill, ...]:
    """Return packaged skills in stable name order."""

    skills: list[BundledSkill] = []
    for skill_dir in sorted(_skills_root().iterdir(), key=lambda entry: entry.name):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        skill_file = skill_dir.joinpath("SKILL.md")
        if not skill_file.is_file():
            continue
        skills.append(
            _parse_skill_metadata(
                skill_file.read_text(encoding="utf-8"),
                folder_name=skill_dir.name,
            )
        )
    return tuple(skills)


def _skill_dir(name: str) -> Traversable:
    normalized = name.strip()
    if normalized != name or not normalized:
        raise ValueError(f"unknown Meridian skill: {name}")
    known_names = {skill.name for skill in list_bundled_skills()}
    if normalized not in known_names:
        available = ", ".join(sorted(known_names))
        raise ValueError(f"unknown Meridian skill {name!r}; choose one of: {available}")
    return _skills_root().joinpath(normalized)


def _resource_parts(resource_path: str) -> tuple[str, ...]:
    if "\\" in resource_path:
        raise ValueError("resource path must use '/' and stay inside the skill folder")
    path = PurePosixPath(resource_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("resource path must be relative and stay inside the skill folder")
    return path.parts


def read_bundled_skill(name: str, resource_path: str | None = None) -> str:
    """Read a skill's ``SKILL.md`` or one resource beneath its folder."""

    skill_dir = _skill_dir(name)
    target = skill_dir.joinpath("SKILL.md")
    if resource_path is not None:
        target = skill_dir.joinpath(*_resource_parts(resource_path))
    if not target.is_file():
        requested = resource_path or "SKILL.md"
        raise ValueError(f"resource not found for Meridian skill {name!r}: {requested}")
    return target.read_text(encoding="utf-8")


__all__ = ["BundledSkill", "list_bundled_skills", "read_bundled_skill"]

