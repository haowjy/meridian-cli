from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.mars_cli import (
    MARS_AGENT_NOT_FOUND_CODES,
    MarsAgentNotFoundError,
    MarsRoot,
    legacy_user_root,
    mars_content_args,
    parse_mars_root,
    resolve_mars_executable,
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
    scope: Literal["root", "fallback", "provided"] = "root"
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


def _mars_cwd(project_root: Path | None) -> Path:
    """Where Mars runs, so it chooses the root as it would for the user there."""

    return resolve_project_root_resolution(project_root).project_root


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
        result = subprocess.run(
            [executable, "outdated", "--json"],
            cwd=_mars_cwd(project_root),
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


def _entry_scope(value: object) -> Literal["root", "fallback", "provided"]:
    if value == "fallback":
        return "fallback"
    if value == "provided":
        return "provided"
    return "root"


def mars_resolve_agent(project_root: Path, name: str) -> Path:
    """The profile file Mars resolves for agent *name*, the one a launch bundle reads.

    Raises ``MarsAgentNotFoundError`` when no content layer has it, with Mars's message, and
    ``ValueError`` when Mars cannot answer.
    """

    executable = resolve_mars_executable()
    if executable is None:
        raise ValueError("mars agents show failed: mars is not installed")
    command = [executable, "agents", "show", name, "--json", *mars_content_args()]
    try:
        result = subprocess.run(
            command,
            cwd=_mars_cwd(project_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"mars agents show failed: {exc}") from exc
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        payload = None
    row = cast("dict[str, object]", payload) if isinstance(payload, dict) else {}
    error = row.get("error")
    if isinstance(error, dict):
        error_row = cast("dict[str, object]", error)
        message = str(error_row.get("message") or f"agent `{name}` not found")
        code = str(error_row.get("code"))
        if code in MARS_AGENT_NOT_FOUND_CODES:
            raise MarsAgentNotFoundError(code, message)
        raise ValueError(f"mars agents show failed: {message}")
    source_path = row.get("source_path")
    if result.returncode != 0 or not isinstance(source_path, str) or not source_path.strip():
        detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
        raise ValueError(f"mars agents show failed: {detail}")
    return Path(source_path)


class MarsAgentInventory(BaseModel):
    """``mars agents list``: every agent the lookup layers hold, and the root Mars chose."""

    model_config = ConfigDict(frozen=True)

    # None from a Mars that predates reporting it.
    root: MarsRoot | None
    agents: tuple[MarsAgentEntry, ...]


def mars_agent_inventory(project_root: Path | None) -> MarsAgentInventory:
    """List every agent profile via ``mars agents list``.

    Raises ``ValueError`` when Mars cannot answer: the listing is the caller's whole
    result, so an empty answer would be a wrong one.
    """

    executable = resolve_mars_executable()
    if executable is None:
        raise ValueError("mars agents list failed: mars is not installed")
    try:
        result = subprocess.run(
            [executable, "agents", "list", "--json", *mars_content_args()],
            cwd=_mars_cwd(project_root),
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
        payload: object = json.loads(result.stdout)
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
                scope=_entry_scope(row.get("scope")),
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
    return MarsAgentInventory(
        root=parse_mars_root(cast("object", payload)),
        agents=tuple(sorted(entries, key=lambda entry: entry.name)),
    )


def mars_list_agents(project_root: Path | None) -> tuple[MarsAgentEntry, ...]:
    return mars_agent_inventory(project_root).agents


def mars_installs_agents_natively(project_root: Path, harness: str) -> bool:
    """Whether the last sync installed at least one agent natively into ``harness``.

    The harness's own agent tool is then the project's delegation mechanism, so Meridian
    leaves it usable. Mars cannot answer: not enabled.
    """

    try:
        agents = mars_list_agents(project_root)
    except ValueError:
        _logger.debug("mars agents list unavailable", exc_info=True)
        return False
    return any(harness in agent.native_harnesses for agent in agents)


@dataclass(frozen=True)
class MarsRootReport:
    """What ``meridian config``, ``context`` and ``doctor`` report about Mars here."""

    root: MarsRoot | None
    # ``~/.meridian/mars`` while lookups still read it as ``--fallback-root``.
    fallback: Path | None

    @property
    def fallback_label(self) -> Literal["active", "n/a"]:
        return "n/a" if self.fallback is None else "active"

    @property
    def user_root(self) -> Path | None:
        """The fallback beneath a project; None at the user level ("same as primary")."""

        return None if self.root is not None and self.root.kind == "user" else self.fallback


def mars_root_report(project_root: Path | None) -> MarsRootReport:
    legacy = legacy_user_root()
    return MarsRootReport(
        root=mars_root(project_root),
        fallback=legacy.path if legacy is not None and legacy.active else None,
    )


def mars_root(project_root: Path | None) -> MarsRoot | None:
    """The root Mars chooses for a lookup here, or None when Mars cannot answer."""

    try:
        return mars_agent_inventory(project_root).root
    except ValueError:
        _logger.debug("mars root unavailable", exc_info=True)
        return None


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
    "MarsAgentInventory",
    "MarsRootReport",
    "UpgradeAvailability",
    "check_upgrade_availability",
    "format_upgrade_availability",
    "mars_agent_inventory",
    "mars_installs_agents_natively",
    "mars_list_agents",
    "mars_resolve_agent",
    "mars_root",
    "mars_root_report",
]
