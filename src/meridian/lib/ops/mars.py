from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.mars_roots import (
    MarsRoots,
    ensure_resolved_mars_root,
    mars_fallback_args,
    resolve_mars_roots,
)
from meridian.lib.catalog.mars_roots import (
    resolve_mars_executable as _resolve_mars_executable,
)
from meridian.lib.config.project_root import resolve_project_root_resolution

_logger = logging.getLogger(__name__)


class UpgradeAvailability(BaseModel):
    """Update availability summary for pinned Mars dependencies."""

    model_config = ConfigDict(frozen=True)

    within_constraint: tuple[str, ...] = ()
    beyond_constraint: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.within_constraint) + len(self.beyond_constraint)


class MarsAgentEntry(BaseModel):
    """One Mars agent inventory entry with content-layer provenance."""

    model_config = ConfigDict(frozen=True)

    name: str
    scope: Literal["root", "fallback"] = "root"
    source_path: str | None = None
    description: str = ""
    mode: Literal["primary", "subagent"] = "subagent"
    # The profile's advisory `subagents:` list.
    subagents: tuple[str, ...] = ()
    model_invocable: bool = True
    # Harnesses the last sync installed the agent into natively.
    native_harnesses: tuple[str, ...] = ()
    # Named in `[settings.meridian.fanout]`: Meridian launches it even where native.
    fanout: bool = False


def resolve_mars_executable() -> str | None:
    """Prefer the Mars binary from this install environment over PATH."""
    return _resolve_mars_executable()


def _prepare_mars_roots(project_root: Path | None, *, executable: str) -> MarsRoots:
    project = resolve_project_root_resolution(project_root).project_root
    roots = resolve_mars_roots(project)
    ensure_resolved_mars_root(roots, executable=executable)
    return roots


def _is_head_constraint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().upper() == "HEAD"


def check_upgrade_availability(project_root: Path | None = None) -> UpgradeAvailability | None:
    """Classify dependency upgrades from ``mars outdated --json``.

    Returns ``None`` when the check cannot be completed (missing binary, command
    failure, malformed JSON, timeout). HEAD-constrained rows are ignored because
    they track moving refs and would produce noisy perpetual updates. Up-to-date
    rows are ignored, and malformed or unchecked (``?``) rows are skipped.
    """

    executable = resolve_mars_executable()
    if executable is None:
        return None

    try:
        mars_roots = _prepare_mars_roots(project_root, executable=executable)
    except RuntimeError:
        return None

    command = [executable, "outdated", "--json", "--root", mars_roots.primary.as_posix()]

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
        # Mars prints "?" for versions it did not check (overridden or
        # plugin-selected dependency, or version listing failed) and "-" for no lock.
        if "?" in (updateable_s, latest_s) or locked_s == "-":
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


def mars_list_agents(project_root: Path) -> tuple[MarsAgentEntry, ...]:
    """List every installed agent profile via ``mars agents list``.

    Raises ``ValueError`` when Mars cannot answer: the listing is the caller's whole
    result, so an empty answer would be a wrong one.
    """

    executable = resolve_mars_executable()
    if executable is None:
        raise ValueError("mars agents list failed: mars is not installed")

    try:
        mars_roots = _prepare_mars_roots(project_root, executable=executable)
    except RuntimeError as exc:
        raise ValueError(f"mars agents list failed: {exc}") from exc

    command = [
        executable,
        "agents",
        "list",
        "--json",
        "--root",
        mars_roots.primary.as_posix(),
    ]
    command.extend(mars_fallback_args(mars_roots, executable=executable))

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"mars agents list failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
        raise ValueError(f"mars agents list failed: {detail}")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"mars agents list returned malformed JSON: {exc}") from exc
    agents_raw = (
        cast("dict[str, object]", payload).get("agents") if isinstance(payload, dict) else None
    )
    if not isinstance(agents_raw, list):
        raise ValueError("mars agents list returned no `agents` array")

    entries: list[MarsAgentEntry] = []
    seen: set[str] = set()
    for row_obj in cast("list[object]", agents_raw):
        if not isinstance(row_obj, dict):
            continue
        row = cast("dict[str, object]", row_obj)
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or name.strip() in seen:
            continue
        normalized = name.strip()
        seen.add(normalized)
        source_path = row.get("source_path")
        description = row.get("description")
        entries.append(
            MarsAgentEntry(
                name=normalized,
                scope="fallback" if row.get("scope") == "fallback" else "root",
                source_path=source_path.strip()
                if isinstance(source_path, str) and source_path.strip()
                else None,
                description=description.strip() if isinstance(description, str) else "",
                mode="primary" if row.get("mode") == "primary" else "subagent",
                subagents=_string_tuple(row.get("subagents")),
                model_invocable=row.get("model-invocable") is not False,
                native_harnesses=_string_tuple(row.get("native-harnesses")),
                fanout=row.get("fanout") is True,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.name))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in cast("list[object]", value) if isinstance(item, str))


def format_upgrade_availability(
    availability: UpgradeAvailability,
    *,
    style: Literal["hint", "warning"] = "hint",
) -> tuple[str, ...]:

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
    "MarsAgentEntry",
    "UpgradeAvailability",
    "check_upgrade_availability",
    "format_upgrade_availability",
    "mars_list_agents",
    "resolve_mars_executable",
]
