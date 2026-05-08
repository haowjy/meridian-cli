"""Workspace topology file parsing and evaluated snapshot state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths
from meridian.lib.state.paths import (
    _load_workspace_table,  # pyright: ignore[reportPrivateUsage]
    _merge_nested_dicts,  # pyright: ignore[reportPrivateUsage]
)

WorkspaceStatus = Literal["none", "present", "invalid"]
WorkspaceFindingCode = Literal[
    "workspace_invalid",
    "workspace_unknown_key",
    "workspace_missing_root",
    "workspace_local_missing_root",
]

_WORKSPACE_PATH_KEY = "path"
_ENTRY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class WorkspaceFinding(BaseModel):
    """Structured workspace finding surfaced by config/doctor output."""

    model_config = ConfigDict(frozen=True)

    code: WorkspaceFindingCode
    message: str
    payload: dict[str, object] | None = None


WorkspaceRootSource = Literal["committed", "local", "merged"]


class WorkspaceEntryConfig(BaseModel):
    """One named `[workspace.<name>]` entry from repo config."""

    model_config = ConfigDict(frozen=True)

    path: str
    extra_keys: dict[str, object] = Field(default_factory=dict)


class ResolvedWorkspaceRoot(BaseModel):
    """Evaluated workspace root entry with identity and filesystem state."""

    model_config = ConfigDict(frozen=True)

    name: str
    declared_path: str
    resolved_path: Path
    enabled: bool
    exists: bool
    source: WorkspaceRootSource


class WorkspaceSnapshot(BaseModel):
    """Shared workspace read model consumed by config/doctor/launch code."""

    model_config = ConfigDict(frozen=True)

    status: WorkspaceStatus
    source_paths: tuple[Path, ...] = ()
    roots: tuple[ResolvedWorkspaceRoot, ...] = ()
    findings: tuple[WorkspaceFinding, ...] = ()

    @property
    def roots_count(self) -> int:
        return len(self.roots)

    @property
    def enabled_roots_count(self) -> int:
        return sum(1 for root in self.roots if root.enabled)

    @property
    def missing_roots_count(self) -> int:
        return sum(1 for root in self.roots if root.enabled and not root.exists)

    @classmethod
    def none(cls) -> WorkspaceSnapshot:
        return cls(status="none")

    @classmethod
    def invalid(cls, *, path: Path, message: str) -> WorkspaceSnapshot:
        normalized = message.strip() or "Workspace file is invalid."
        return cls(
            status="invalid",
            source_paths=(path,),
            findings=(
                WorkspaceFinding(
                    code="workspace_invalid",
                    message=normalized,
                    payload={"path": path.as_posix()},
                ),
            ),
        )


def get_projectable_roots(snapshot: WorkspaceSnapshot) -> tuple[Path, ...]:
    """Return ordered enabled existing roots for projection."""

    return tuple(root.resolved_path for root in snapshot.roots if root.enabled and root.exists)


def _resolve_named_workspace_root_path(
    *,
    project_root: Path,
    declared_path: str,
) -> Path:
    candidate = Path(declared_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _parse_workspace_entry(
    *,
    name: str,
    raw_entry: object,
    source_path: Path,
) -> WorkspaceEntryConfig:
    if not _ENTRY_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Invalid workspace schema: "
            f"workspace entry name '{name}' in '{source_path.as_posix()}' must match "
            r"^[a-z][a-z0-9_-]*$."
        )
    if not isinstance(raw_entry, dict):
        raise ValueError(
            "Invalid workspace schema: "
            f"'workspace.{name}' in '{source_path.as_posix()}' must be a table."
        )

    entry = cast("dict[str, object]", raw_entry)
    if _WORKSPACE_PATH_KEY not in entry:
        raise ValueError(
            "Invalid workspace schema: "
            f"'workspace.{name}.path' in '{source_path.as_posix()}' is required."
        )
    raw_path = entry[_WORKSPACE_PATH_KEY]
    if not isinstance(raw_path, str):
        raise ValueError(
            "Invalid workspace schema: "
            f"'workspace.{name}.path' in '{source_path.as_posix()}' must be a string."
        )
    normalized_path = raw_path.strip()
    if not normalized_path:
        raise ValueError(
            "Invalid workspace schema: "
            f"'workspace.{name}.path' in '{source_path.as_posix()}' must be non-empty."
        )

    extra_keys = {key: value for key, value in entry.items() if key != _WORKSPACE_PATH_KEY}
    return WorkspaceEntryConfig(path=normalized_path, extra_keys=extra_keys)


def _parse_workspace_layer(
    *,
    raw_workspace: dict[str, object],
    source_path: Path,
) -> dict[str, WorkspaceEntryConfig]:
    entries: dict[str, WorkspaceEntryConfig] = {}
    for name, raw_entry in raw_workspace.items():
        entries[name] = _parse_workspace_entry(
            name=name,
            raw_entry=raw_entry,
            source_path=source_path,
        )
    return entries


def _unknown_workspace_key_findings(
    *,
    entries_by_path: list[tuple[Path, dict[str, WorkspaceEntryConfig]]],
) -> list[WorkspaceFinding]:
    unknown_keys: list[str] = []
    for _source_path, entries in entries_by_path:
        for name, entry in entries.items():
            unknown_keys.extend(
                f"workspace.{name}.{key}" for key in sorted(entry.extra_keys.keys())
            )
    if not unknown_keys:
        return []
    return [
        WorkspaceFinding(
            code="workspace_unknown_key",
            message=(
                "Workspace config contains unknown keys: "
                + ", ".join(unknown_keys)
                + "."
            ),
            payload={"keys": unknown_keys},
        )
    ]


def _evaluate_named_workspace_config(
    *,
    project_root: Path,
    committed_path: Path,
    local_path: Path,
    committed_entries: dict[str, WorkspaceEntryConfig],
    local_entries: dict[str, WorkspaceEntryConfig],
    source_paths: tuple[Path, ...],
    initial_findings: tuple[WorkspaceFinding, ...] = (),
) -> WorkspaceSnapshot:
    committed_raw = {
        name: entry.model_dump(exclude={"extra_keys"})
        for name, entry in committed_entries.items()
    }
    local_raw = {
        name: entry.model_dump(exclude={"extra_keys"})
        for name, entry in local_entries.items()
    }
    merged_raw = _merge_nested_dicts(
        cast("dict[str, object]", committed_raw),
        cast("dict[str, object]", local_raw),
    )

    ordered_names = list(committed_entries.keys())
    ordered_names.extend(name for name in local_entries if name not in committed_entries)

    roots: list[ResolvedWorkspaceRoot] = []
    findings: list[WorkspaceFinding] = list(initial_findings)
    missing_committed_roots: list[str] = []
    for name in ordered_names:
        raw_entry = merged_raw[name]
        entry = _parse_workspace_entry(
            name=name,
            raw_entry=raw_entry,
            source_path=local_path if name in local_entries else committed_path,
        )
        source: WorkspaceRootSource
        if name in committed_entries and name in local_entries:
            source = "merged"
        elif name in local_entries:
            source = "local"
        else:
            source = "committed"

        resolved_path = _resolve_named_workspace_root_path(
            project_root=project_root,
            declared_path=entry.path,
        )
        exists = resolved_path.is_dir()
        roots.append(
            ResolvedWorkspaceRoot(
                name=name,
                declared_path=entry.path,
                resolved_path=resolved_path,
                enabled=True,
                exists=exists,
                source=source,
            )
        )
        if exists:
            continue
        if source in {"local", "merged"}:
            findings.append(
                WorkspaceFinding(
                    code="workspace_local_missing_root",
                    message=(
                        f"Local workspace root '{name}' does not exist: "
                        f"{resolved_path.as_posix()}."
                    ),
                    payload={"name": name, "path": resolved_path.as_posix()},
                )
            )
            continue
        missing_committed_roots.append(resolved_path.as_posix())

    if missing_committed_roots:
        findings.append(
            WorkspaceFinding(
                code="workspace_missing_root",
                message=(
                    "Enabled workspace roots are missing: "
                    + ", ".join(missing_committed_roots)
                ),
                payload={"roots": missing_committed_roots},
            )
        )

    return WorkspaceSnapshot(
        status="present",
        source_paths=source_paths,
        roots=tuple(roots),
        findings=tuple(findings),
    )


def resolve_workspace_snapshot(
    project_root: Path,
    *,
    config_paths: ProjectConfigPaths | None = None,
) -> WorkspaceSnapshot:
    """Resolve canonical workspace snapshot from project-root paths."""

    resolved_config_paths = config_paths or resolve_project_config_paths(project_root)
    committed_path = resolved_config_paths.meridian_toml
    local_path = resolved_config_paths.meridian_local_toml

    raw_layers: list[tuple[Path, dict[str, object]]] = []
    for config_path in (committed_path, local_path):
        try:
            workspace_table = _load_workspace_table(config_path)
        except ValueError as exc:
            return WorkspaceSnapshot.invalid(path=config_path.resolve(), message=str(exc))
        if workspace_table is not None:
            raw_layers.append((config_path.resolve(), workspace_table))

    if not raw_layers:
        return WorkspaceSnapshot.none()

    failed_source_path: Path | None = None
    try:
        committed_entries: dict[str, WorkspaceEntryConfig] = {}
        local_entries: dict[str, WorkspaceEntryConfig] = {}
        parsed_layers: list[tuple[Path, dict[str, WorkspaceEntryConfig]]] = []
        for source_path, raw_workspace in raw_layers:
            failed_source_path = source_path
            entries = _parse_workspace_layer(
                raw_workspace=raw_workspace,
                source_path=source_path,
            )
            parsed_layers.append((source_path, entries))
            if source_path == committed_path.resolve():
                committed_entries = entries
            else:
                local_entries = entries
    except ValueError as exc:
        return WorkspaceSnapshot.invalid(
            path=failed_source_path or raw_layers[0][0],
            message=str(exc),
        )

    findings = _unknown_workspace_key_findings(entries_by_path=parsed_layers)
    return _evaluate_named_workspace_config(
        project_root=resolved_config_paths.project_root,
        committed_path=committed_path.resolve(),
        local_path=local_path.resolve(),
        committed_entries=committed_entries,
        local_entries=local_entries,
        source_paths=tuple(path for path, _raw_workspace in raw_layers),
        initial_findings=tuple(findings),
    )


__all__ = [
    "ResolvedWorkspaceRoot",
    "WorkspaceEntryConfig",
    "WorkspaceFinding",
    "WorkspaceRootSource",
    "WorkspaceSnapshot",
    "WorkspaceStatus",
    "get_projectable_roots",
    "resolve_workspace_snapshot",
]
