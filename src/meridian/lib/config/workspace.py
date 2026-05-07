"""Workspace topology file parsing and evaluated snapshot state."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.project_paths import resolve_project_config_paths
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
    "workspace_legacy_file_present",
    "workspace_deprecated_legacy",
]

_CONTEXT_ROOTS_KEY = "context-roots"
_CONTEXT_ROOT_PATH_KEY = "path"
_CONTEXT_ROOT_ENABLED_KEY = "enabled"
_WORKSPACE_PATH_KEY = "path"
_ENTRY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class WorkspaceFinding(BaseModel):
    """Structured workspace finding surfaced by config/doctor output."""

    model_config = ConfigDict(frozen=True)

    code: WorkspaceFindingCode
    message: str
    payload: dict[str, object] | None = None


WorkspaceRootSource = Literal["committed", "local", "merged", "legacy"]


class WorkspaceRootConfig(BaseModel):
    """One parsed legacy `[[context-roots]]` workspace entry."""

    model_config = ConfigDict(frozen=True)

    path: str
    enabled: bool = True
    extra_keys: dict[str, object] = Field(default_factory=dict)


class WorkspaceEntryConfig(BaseModel):
    """One named `[workspace.<name>]` entry from repo config."""

    model_config = ConfigDict(frozen=True)

    path: str
    extra_keys: dict[str, object] = Field(default_factory=dict)


class WorkspaceConfig(BaseModel):
    """Parsed `workspace.local.toml` document (no filesystem evaluation)."""

    model_config = ConfigDict(frozen=True)

    path: Path
    context_roots: tuple[WorkspaceRootConfig, ...]
    unknown_top_level_keys: tuple[str, ...] = ()


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

    return tuple(
        root.resolved_path
        for root in snapshot.roots
        if root.enabled and root.exists
    )


def _parse_context_root(
    *,
    raw_entry: object,
    entry_index: int,
) -> WorkspaceRootConfig:
    if not isinstance(raw_entry, dict):
        raise ValueError(
            f"Invalid workspace schema: '{_CONTEXT_ROOTS_KEY}[{entry_index}]' must be a table."
        )
    entry = cast("dict[str, object]", raw_entry)

    if _CONTEXT_ROOT_PATH_KEY not in entry:
        raise ValueError(
            f"Invalid workspace schema: '{_CONTEXT_ROOTS_KEY}[{entry_index}].path' is required."
        )
    raw_path = entry[_CONTEXT_ROOT_PATH_KEY]
    if not isinstance(raw_path, str):
        raise ValueError(
            "Invalid workspace schema: "
            f"'{_CONTEXT_ROOTS_KEY}[{entry_index}].path' must be a string."
        )
    normalized_path = raw_path.strip()
    if not normalized_path:
        raise ValueError(
            "Invalid workspace schema: "
            f"'{_CONTEXT_ROOTS_KEY}[{entry_index}].path' must be non-empty."
        )

    raw_enabled = entry.get(_CONTEXT_ROOT_ENABLED_KEY, True)
    if not isinstance(raw_enabled, bool):
        raise ValueError(
            f"Invalid workspace schema: '{_CONTEXT_ROOTS_KEY}[{entry_index}].enabled' "
            "must be a boolean."
        )

    extra_keys = {
        key: value
        for key, value in entry.items()
        if key not in {_CONTEXT_ROOT_PATH_KEY, _CONTEXT_ROOT_ENABLED_KEY}
    }
    return WorkspaceRootConfig(
        path=normalized_path,
        enabled=raw_enabled,
        extra_keys=extra_keys,
    )


def parse_workspace_config(path: Path) -> WorkspaceConfig:
    """Parse one `workspace.local.toml` file into a structured document model."""

    try:
        payload_obj = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid workspace TOML: {exc}") from exc

    payload = cast("dict[str, object]", payload_obj)

    raw_context_roots = payload.get(_CONTEXT_ROOTS_KEY, [])
    if not isinstance(raw_context_roots, list):
        raise ValueError(
            f"Invalid workspace schema: '{_CONTEXT_ROOTS_KEY}' must be an array of tables."
        )

    context_roots: list[WorkspaceRootConfig] = []
    for index, raw_entry in enumerate(cast("list[object]", raw_context_roots), start=1):
        context_roots.append(_parse_context_root(raw_entry=raw_entry, entry_index=index))

    unknown_top_level_keys = tuple(sorted(key for key in payload if key != _CONTEXT_ROOTS_KEY))
    return WorkspaceConfig(
        path=path.resolve(),
        context_roots=tuple(context_roots),
        unknown_top_level_keys=unknown_top_level_keys,
    )


def _resolve_workspace_root_path(*, workspace_file: Path, declared_path: str) -> Path:
    candidate = Path(declared_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_file.parent / candidate
    return candidate.resolve()


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

    extra_keys = {
        key: value
        for key, value in entry.items()
        if key != _WORKSPACE_PATH_KEY
    }
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
    # Keep the merge implementation aligned with other config sections while
    # deriving deterministic projection order from the per-layer insertion order.
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
        if source in {"local", "merged"} and not exists:
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

    return WorkspaceSnapshot(
        status="present",
        source_paths=source_paths,
        roots=tuple(roots),
        findings=tuple(findings),
    )


def _unknown_key_identifiers(config: WorkspaceConfig) -> tuple[str, ...]:
    keys: list[str] = list(config.unknown_top_level_keys)
    for index, root in enumerate(config.context_roots, start=1):
        keys.extend(
            f"{_CONTEXT_ROOTS_KEY}[{index}].{key}"
            for key in sorted(root.extra_keys.keys())
        )
    return tuple(keys)


def _evaluate_workspace_config(config: WorkspaceConfig) -> WorkspaceSnapshot:
    resolved_roots: list[ResolvedWorkspaceRoot] = []
    for index, root in enumerate(config.context_roots, start=1):
        resolved_path = _resolve_workspace_root_path(
            workspace_file=config.path,
            declared_path=root.path,
        )
        resolved_roots.append(
            ResolvedWorkspaceRoot(
                name=f"legacy-{index}",
                declared_path=root.path,
                resolved_path=resolved_path,
                enabled=root.enabled,
                exists=resolved_path.is_dir(),
                source="legacy",
            )
        )
    roots = tuple(resolved_roots)
    unknown_keys = _unknown_key_identifiers(config)

    findings: list[WorkspaceFinding] = []
    if unknown_keys:
        findings.append(
            WorkspaceFinding(
                code="workspace_unknown_key",
                message=(
                    "Workspace file contains unknown keys: "
                    + ", ".join(unknown_keys)
                    + "."
                ),
                payload={"keys": list(unknown_keys)},
            )
        )

    missing_roots = [
        root.resolved_path.as_posix()
        for root in roots
        if root.enabled and not root.exists
    ]
    if missing_roots:
        findings.append(
            WorkspaceFinding(
                code="workspace_missing_root",
                message="Enabled workspace roots are missing: " + ", ".join(missing_roots),
                payload={"roots": missing_roots},
            )
        )

    return WorkspaceSnapshot(
        status="present",
        source_paths=(config.path,),
        roots=roots,
        findings=tuple(findings),
    )


def resolve_workspace_snapshot(project_root: Path) -> WorkspaceSnapshot:
    """Resolve canonical workspace snapshot from project-root paths."""

    config_paths = resolve_project_config_paths(project_root)
    committed_path = config_paths.meridian_toml
    local_path = config_paths.meridian_local_toml
    legacy_workspace_path = config_paths.workspace_local_toml

    raw_layers: list[tuple[Path, dict[str, object]]] = []
    for config_path in (committed_path, local_path):
        try:
            workspace_table = _load_workspace_table(config_path)
        except ValueError as exc:
            return WorkspaceSnapshot.invalid(path=config_path.resolve(), message=str(exc))
        if workspace_table is not None:
            raw_layers.append((config_path.resolve(), workspace_table))

    if raw_layers:
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
        if legacy_workspace_path.exists():
            findings.append(
                WorkspaceFinding(
                    code="workspace_legacy_file_present",
                    message=(
                        "Legacy workspace.local.toml is present but ignored because "
                        "[workspace] config exists; migrate entries to meridian.local.toml."
                    ),
                    payload={"path": legacy_workspace_path.resolve().as_posix()},
                )
            )

        return _evaluate_named_workspace_config(
            project_root=config_paths.project_root,
            committed_path=committed_path.resolve(),
            local_path=local_path.resolve(),
            committed_entries=committed_entries,
            local_entries=local_entries,
            source_paths=tuple(path for path, _raw_workspace in raw_layers),
            initial_findings=tuple(findings),
        )

    workspace_path = legacy_workspace_path
    if not workspace_path.exists():
        return WorkspaceSnapshot.none()
    if not workspace_path.is_file():
        return WorkspaceSnapshot.invalid(
            path=workspace_path.resolve(),
            message=f"Workspace path '{workspace_path.as_posix()}' exists but is not a file.",
        )
    try:
        config = parse_workspace_config(workspace_path)
    except ValueError as exc:
        return WorkspaceSnapshot.invalid(path=workspace_path.resolve(), message=str(exc))
    snapshot = _evaluate_workspace_config(config)
    legacy_findings = (
        WorkspaceFinding(
            code="workspace_deprecated_legacy",
            message=(
                "workspace.local.toml is deprecated; migrate workspace roots to "
                "[workspace] entries in meridian.local.toml."
            ),
            payload={"path": workspace_path.resolve().as_posix()},
        ),
        WorkspaceFinding(
            code="workspace_legacy_file_present",
            message=(
                "Legacy workspace.local.toml is present; migrate workspace roots to "
                "[workspace] entries in meridian.local.toml."
            ),
            payload={"path": workspace_path.resolve().as_posix()},
        ),
    )
    return snapshot.model_copy(update={"findings": legacy_findings + snapshot.findings})


__all__ = [
    "ResolvedWorkspaceRoot",
    "WorkspaceConfig",
    "WorkspaceEntryConfig",
    "WorkspaceFinding",
    "WorkspaceRootConfig",
    "WorkspaceRootSource",
    "WorkspaceSnapshot",
    "WorkspaceStatus",
    "get_projectable_roots",
    "parse_workspace_config",
    "resolve_workspace_snapshot",
]
