"""Provide and read Meridian's packaged operational skills."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import TextIO, cast

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
class WheelSkillSource:
    """Read bundled skills from the installed wheel.

    ``refusal`` is Mars's reason for refusing the bundle. Mars reports a broken ``mars.toml`` the
    same way, so the caller's own command usually fails with the same message; each caller prints
    it with ``report_provide_refusal`` only once that command has succeeded.
    """

    sync_pending: bool = False
    refusal: str | None = None


@dataclass(frozen=True)
class ProvidedSkillSource:
    """Mars-provided skill names and their common parent directory."""

    skills_dir: Path
    provided: frozenset[str]
    sync_pending: bool = False


BundledSkillSource = WheelSkillSource | ProvidedSkillSource
WHEEL = WheelSkillSource()
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
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stderr: TextIO | None = None,
) -> BundledSkillSource:
    """Ask Mars to install this build's bundled skills into ``primary_root``.

    Falls back to the wheel when Mars cannot provide them. Mars's warnings print here;
    a refusal is returned on the source instead (``report_provide_refusal``).
    """

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
        result = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return WHEEL
    if result.returncode != 0:
        # A Mars that refused the bundle (an invalid skill, a newer provided.lock) says why in a
        # JSON error on stdout; a Mars without `provide` prints only usage text, so stays quiet.
        return WheelSkillSource(refusal=_mars_error_message(result.stdout))

    try:
        payload_obj = json.loads(result.stdout)
    except ValueError:
        return WHEEL
    if not isinstance(payload_obj, dict):
        return WHEEL
    payload = cast("dict[str, object]", payload_obj)
    status = payload.get("status")
    if status == "sync-pending":
        return WheelSkillSource(sync_pending=True)
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
                    print(warning, file=sys.stderr if stderr is None else stderr)
    return ProvidedSkillSource(
        skills_dir=primary_root / ".mars" / "skills",
        provided=provided,
    )


def report_provide_refusal(source: BundledSkillSource, *, stderr: TextIO | None = None) -> None:
    """Warn that Mars refused the bundle and Meridian fell back to its built-in skills."""

    if isinstance(source, WheelSkillSource) and source.refusal is not None:
        print(
            f"warning: mars provide failed, using Meridian's built-in skills: {source.refusal}",
            file=sys.stderr if stderr is None else stderr,
        )


def _mars_error_message(stdout: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = cast("dict[str, object]", payload).get("error")
    if not isinstance(error, dict):
        return None
    message = cast("dict[str, object]", error).get("message")
    return message if isinstance(message, str) and message.strip() else None


def _skill_dir(name: str, source: BundledSkillSource) -> Traversable:
    normalized = name.strip()
    if normalized != name or not normalized:
        raise ValueError(f"unknown Meridian skill: {name}")
    known_names = {skill.name for skill in list_bundled_skills()}
    if normalized not in known_names:
        available = ", ".join(sorted(known_names))
        raise ValueError(f"unknown Meridian skill {name!r}; choose one of: {available}")
    if isinstance(source, ProvidedSkillSource) and normalized in source.provided:
        provided_dir = source.skills_dir / normalized
        skill_file = provided_dir / "SKILL.md"
        if skill_file.is_file():
            try:
                _parse_skill_metadata(
                    skill_file.read_text(encoding="utf-8"),
                    folder_name=normalized,
                )
            except (OSError, RuntimeError):
                pass
            else:
                return provided_dir
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
    text = skill_file.read_text(encoding="utf-8")
    metadata = _parse_skill_metadata(text, folder_name=name)
    post = frontmatter.loads(text)
    return SkillContent(
        name=name,
        description=metadata.description,
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
    "ProvidedSkillSource",
    "WheelSkillSource",
    "list_bundled_skills",
    "load_bundled_skill",
    "provide_bundled_skills",
    "read_bundled_skill_resource",
    "report_provide_refusal",
]

