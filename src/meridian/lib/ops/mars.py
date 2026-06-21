"""Shared Mars helpers used by Meridian operations and CLI wrappers."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)


class UpgradeAvailability(BaseModel):
    """Update availability summary for pinned Mars dependencies."""

    model_config = ConfigDict(frozen=True)

    within_constraint: tuple[str, ...] = ()
    beyond_constraint: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.within_constraint) + len(self.beyond_constraint)


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over PATH."""

    # Keep the wrapper path intact: uv tool scripts point at a symlinked Python
    # binary, and resolving it jumps out of the tool environment where sibling
    # scripts (including `mars`) live.
    scripts_dir = Path(sys.executable).parent
    for name in ("mars", "mars.exe"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mars")


def _is_head_constraint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().upper() == "HEAD"


def check_upgrade_availability(project_root: Path | None = None) -> UpgradeAvailability | None:
    """Classify dependency upgrades from ``mars outdated --json``.

    Returns ``None`` when the check cannot be completed (missing binary, command
    failure, malformed JSON, timeout). HEAD-constrained rows are ignored because
    they track moving refs and would produce noisy perpetual updates. Up-to-date
    rows are ignored, and malformed rows are skipped.
    """

    executable = resolve_mars_executable()
    if executable is None:
        return None

    command = [executable, "outdated", "--json"]
    if project_root is not None:
        command.extend(["--root", project_root.as_posix()])

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, list):
        return None

    within_constraint: list[str] = []
    beyond_constraint: list[str] = []
    seen: set[str] = set()
    for row_obj in cast("list[object]", payload):
        if not isinstance(row_obj, dict):
            continue
        row = cast("dict[str, object]", row_obj)
        source = row.get("source")
        if not isinstance(source, str):
            continue
        normalized_source = source.strip()
        if not normalized_source:
            continue
        if _is_head_constraint(row.get("constraint")):
            continue

        locked = row.get("locked")
        updateable = row.get("updateable")
        latest = row.get("latest")
        if (
            not isinstance(locked, str)
            or not isinstance(updateable, str)
            or not isinstance(latest, str)
        ):
            continue
        locked_s = locked.strip()
        updateable_s = updateable.strip()
        latest_s = latest.strip()
        if not locked_s or not updateable_s or not latest_s:
            continue
        if normalized_source in seen:
            continue
        if locked_s != updateable_s:
            seen.add(normalized_source)
            within_constraint.append(normalized_source)
            continue
        # Intentionally use normalized string equality, not semver ordering.
        # Rare lockfiles manually moved ahead of upstream may emit a noisy hint.
        if locked_s != latest_s:
            seen.add(normalized_source)
            beyond_constraint.append(normalized_source)

    return UpgradeAvailability(
        within_constraint=tuple(within_constraint),
        beyond_constraint=tuple(beyond_constraint),
    )


def _sorted_unique_strings(values: object) -> tuple[str, ...] | None:
    if not isinstance(values, list):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for item in cast("list[object]", values):
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(normalized)
    return tuple(sorted(names))


def mars_agent_subagents(project_root: Path, agent_name: str) -> tuple[str, ...] | None:
    """Return declared subagent allow-list for *agent_name* via ``mars agents show``.

    Returns ``None`` when mars is missing, the agent is unknown, the call fails, or
    JSON is malformed. An agent that exists with an empty ``subagents`` list returns
    ``()`` so callers can distinguish "no restriction declared" from "could not resolve".
    """

    executable = resolve_mars_executable()
    if executable is None:
        return None

    command = [
        executable,
        "agents",
        "show",
        agent_name,
        "--json",
        "--root",
        project_root.as_posix(),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _logger.warning(
            "mars agents show failed for %r: %s",
            agent_name,
            exc,
        )
        return None

    if result.returncode != 0:
        _logger.warning(
            "mars agents show failed for %r: exit code %s",
            agent_name,
            result.returncode,
        )
        return None

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        _logger.warning(
            "mars agents show returned malformed JSON for %r: %s",
            agent_name,
            exc,
        )
        return None

    if not isinstance(payload, dict):
        _logger.warning(
            "mars agents show returned unexpected JSON for %r: expected object, got %s",
            agent_name,
            type(payload).__name__,
        )
        return None

    subagents = _sorted_unique_strings(cast("dict[str, object]", payload).get("subagents"))
    if subagents is None:
        return ()
    return subagents


def mars_list_subagents(project_root: Path) -> tuple[str, ...]:
    """List subagent profile names via ``mars agents list --mode subagent``.

    Returns ``()`` on any failure (graceful degradation).
    """

    executable = resolve_mars_executable()
    if executable is None:
        return ()

    command = [
        executable,
        "agents",
        "list",
        "--mode",
        "subagent",
        "--json",
        "--root",
        project_root.as_posix(),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _logger.warning("mars agents list --mode subagent failed: %s", exc)
        return ()

    if result.returncode != 0:
        _logger.warning(
            "mars agents list --mode subagent failed: exit code %s",
            result.returncode,
        )
        return ()

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        _logger.warning("mars agents list --mode subagent returned malformed JSON: %s", exc)
        return ()

    if not isinstance(payload, dict):
        _logger.warning(
            "mars agents list --mode subagent returned unexpected JSON: expected object, got %s",
            type(payload).__name__,
        )
        return ()

    agents_raw = cast("dict[str, object]", payload).get("agents")
    if not isinstance(agents_raw, list):
        return ()

    names: list[str] = []
    seen: set[str] = set()
    for row_obj in cast("list[object]", agents_raw):
        if not isinstance(row_obj, dict):
            continue
        name = cast("dict[str, object]", row_obj).get("name")
        if not isinstance(name, str):
            continue
        normalized = name.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(normalized)
    return tuple(sorted(names))


def format_upgrade_availability(
    availability: UpgradeAvailability,
    *,
    style: Literal["hint", "warning"] = "hint",
) -> tuple[str, ...]:
    """Render upgrade availability grouped by available action."""

    lines: list[str] = []
    with_prefix = style == "hint"
    if availability.within_constraint:
        within_count = len(availability.within_constraint)
        within_noun = "update" if within_count == 1 else "updates"
        within_deps = ", ".join(availability.within_constraint)
        if with_prefix:
            prefix = "hint: " if not lines else "      "
        else:
            prefix = "" if not lines else "      "
        lines.append(
            f"{prefix}{within_count} {within_noun} available within your pinned "
            f"constraint: {within_deps}."
        )
        lines.append("      Run `meridian mars upgrade` to apply.")
    if availability.beyond_constraint:
        beyond_count = len(availability.beyond_constraint)
        beyond_noun = "version" if beyond_count == 1 else "versions"
        beyond_deps = ", ".join(availability.beyond_constraint)
        if with_prefix:
            prefix = "hint: " if not lines else "      "
        else:
            prefix = "" if not lines else "      "
        lines.append(
            f"{prefix}{beyond_count} newer {beyond_noun} available beyond your pinned "
            f"constraint: {beyond_deps}."
        )
        lines.append("      Edit mars.toml to bump the version, then run `meridian mars sync`.")
    return tuple(lines)


__all__ = [
    "UpgradeAvailability",
    "check_upgrade_availability",
    "format_upgrade_availability",
    "mars_agent_subagents",
    "mars_list_subagents",
    "resolve_mars_executable",
]
