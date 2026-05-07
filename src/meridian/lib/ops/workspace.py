"""Workspace file operations."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.workspace import parse_workspace_config
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync, resolve_project_authority
from meridian.lib.state.atomic import atomic_write_text

_WORKSPACE_TEMPLATE = """# Workspace topology — local path overrides and additions.
# Override committed [workspace] paths for your local checkout.
#
# [workspace.example]
# path = "../sibling-repo"
"""

_WORKSPACE_SECTION_PATTERN = re.compile(r"^\s*\[workspace(?:\.|\])")
_COMMENTED_WORKSPACE_SECTION_PATTERN = re.compile(r"^\s*#\s*\[workspace(?:\.|\])")


class WorkspaceInitInput(BaseModel):
    """Input model for `workspace init`."""

    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class WorkspaceInitOutput(BaseModel):
    """Result payload for `workspace init`."""

    model_config = ConfigDict(frozen=True)

    path: str
    created: bool
    local_gitignore_path: str | None = None
    local_gitignore_updated: bool = False

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        status = "created" if self.created else "exists"
        lines = [f"{status}: {self.path}"]
        if self.local_gitignore_path is None:
            lines.append("local_gitignore: unavailable")
            return "\n".join(lines)
        coverage = "updated" if self.local_gitignore_updated else "ok"
        lines.append(f"local_gitignore: {self.local_gitignore_path} ({coverage})")
        return "\n".join(lines)


class MigratedEntry(BaseModel):
    """One workspace root migrated from legacy config."""

    model_config = ConfigDict(frozen=True)

    name: str
    original_path: str


class WorkspaceMigrateInput(BaseModel):
    """Input model for `workspace migrate`."""

    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    force: bool = False


class WorkspaceMigrateOutput(BaseModel):
    """Result payload for `workspace migrate`."""

    model_config = ConfigDict(frozen=True)

    path: str
    migrated_entries: int
    entries: tuple[MigratedEntry, ...]
    warnings: tuple[str, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = [f"migrated: {self.migrated_entries} entries -> {self.path}"]
        for entry in self.entries:
            lines.append(f"- workspace.{entry.name}: {entry.original_path}")
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        return "\n".join(lines)


def _resolve_git_dir(project_root: Path) -> Path | None:
    git_entry = project_root / ".git"
    if git_entry.is_dir():
        return git_entry.resolve()
    if not git_entry.is_file():
        return None

    for line in git_entry.read_text(encoding="utf-8").splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        prefix = "gitdir:"
        if not normalized.lower().startswith(prefix):
            break
        raw_target = normalized[len(prefix) :].strip()
        if not raw_target:
            break
        target = Path(raw_target).expanduser()
        if not target.is_absolute():
            target = (project_root / target).resolve()
        if target.is_dir():
            return target
        break
    return None


def _ensure_local_gitignore_entries(
    *,
    project_root: Path,
    entries: tuple[str, ...],
) -> tuple[Path | None, bool]:
    git_dir = _resolve_git_dir(project_root)
    if git_dir is None:
        return None, False

    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    existing_lines = existing_text.splitlines()
    present = {line.strip() for line in existing_lines}
    missing_entries = [entry for entry in entries if entry not in present]
    if not missing_entries:
        return exclude_path, False

    updated_lines = list(existing_lines)
    if updated_lines and updated_lines[-1].strip():
        updated_lines.append("")
    updated_lines.append("# Added by Meridian local workspace init")
    updated_lines.extend(missing_entries)
    updated_text = "\n".join(updated_lines).rstrip() + "\n"
    atomic_write_text(exclude_path, updated_text)
    return exclude_path, True


def workspace_init_sync(payload: WorkspaceInitInput) -> WorkspaceInitOutput:
    authority = resolve_project_authority(payload.project_root)
    project_root = authority.project_root
    project_paths = authority.project_config_paths

    workspace_path = project_paths.meridian_local_toml
    created = False
    if not workspace_path.exists():
        atomic_write_text(workspace_path, _WORKSPACE_TEMPLATE)
        created = True
    elif not workspace_path.is_file():
        raise ValueError(f"Workspace path '{workspace_path.as_posix()}' exists but is not a file.")
    elif not _has_workspace_section_or_scaffold(workspace_path):
        existing_text = workspace_path.read_text(encoding="utf-8")
        updated_text = existing_text.rstrip() + "\n\n" + _WORKSPACE_TEMPLATE
        atomic_write_text(workspace_path, updated_text)
        created = True

    local_gitignore_path, local_gitignore_updated = _ensure_local_gitignore_entries(
        project_root=project_root,
        entries=project_paths.workspace_ignore_targets,
    )

    return WorkspaceInitOutput(
        path=workspace_path.as_posix(),
        created=created,
        local_gitignore_path=(
            local_gitignore_path.as_posix() if local_gitignore_path is not None else None
        ),
        local_gitignore_updated=local_gitignore_updated,
    )


workspace_init = async_from_sync(workspace_init_sync)


def _generate_entry_name(basename: str) -> str | None:
    """Generate entry name from directory basename."""

    if basename in {"", ".", "..", "/"}:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "-", basename.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or None


def _deduplicate_names(names: list[str]) -> list[str]:
    """Deduplicate names with numeric suffixes using case-normalized collisions."""

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        base = name.lower()
        candidate = base
        suffix = 2
        while candidate.lower() in seen:
            candidate = f"{base}-{suffix}"
            suffix += 1
        seen.add(candidate.lower())
        result.append(candidate)
    return result


def _has_workspace_section(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{path.as_posix()}': {exc}") from exc
    return "workspace" in payload


def _has_workspace_section_or_scaffold(path: Path) -> bool:
    if _has_workspace_section(path):
        return True
    for line in path.read_text(encoding="utf-8").splitlines():
        if _COMMENTED_WORKSPACE_SECTION_PATTERN.match(line):
            return True
    return False


def _strip_workspace_sections(text: str) -> str:
    """Remove [workspace] and [workspace.NAME] sections from a TOML document."""

    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^\s*\[", line):
            skipping = bool(_WORKSPACE_SECTION_PATTERN.match(stripped))
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _migration_block(entries: tuple[MigratedEntry, ...]) -> str:
    lines = ["# Auto-migrated from workspace.local.toml by `meridian workspace migrate`."]
    for entry in entries:
        lines.append("")
        lines.append(f"[workspace.{entry.name}]")
        lines.append(f"path = {_toml_string(entry.original_path)}")
    return "\n".join(lines).rstrip() + "\n"


def _provisional_name_warning() -> str:
    return (
        "Review and rename auto-generated workspace entry names before adopting shared "
        "[workspace] conventions in meridian.toml; basename-derived names are provisional "
        "and may not match the canonical names chosen by the project when committed entries "
        "are added."
    )


def _disabled_roots_warning(count: int) -> str:
    return (
        f"Skipped {count} disabled legacy root(s). Disabled roots cannot be represented "
        "in the new [workspace] schema."
    )


def workspace_migrate_sync(payload: WorkspaceMigrateInput) -> WorkspaceMigrateOutput:
    authority = resolve_project_authority(payload.project_root)
    project_paths = authority.project_config_paths

    legacy_path = project_paths.workspace_local_toml
    if not legacy_path.exists():
        raise ValueError(f"Legacy workspace file '{legacy_path.as_posix()}' does not exist.")
    if not legacy_path.is_file():
        raise ValueError(f"Workspace path '{legacy_path.as_posix()}' exists but is not a file.")

    local_path = project_paths.meridian_local_toml
    if local_path.exists() and not local_path.is_file():
        raise ValueError(f"Workspace path '{local_path.as_posix()}' exists but is not a file.")
    if _has_workspace_section(local_path) and not payload.force:
        raise ValueError(
            f"Workspace config already exists in '{local_path.as_posix()}'. "
            "Use --force to overwrite."
        )

    legacy_config = parse_workspace_config(legacy_path)
    enabled_roots = tuple(root for root in legacy_config.context_roots if root.enabled)
    disabled_roots_count = len(legacy_config.context_roots) - len(enabled_roots)
    provisional_names: list[str] = []
    root_counter = 1
    for root in enabled_roots:
        generated = _generate_entry_name(Path(root.path).name)
        if generated is None:
            generated = f"root-{root_counter}"
            root_counter += 1
        provisional_names.append(generated)
    names = _deduplicate_names(provisional_names)
    entries = tuple(
        MigratedEntry(name=name, original_path=root.path)
        for name, root in zip(names, enabled_roots, strict=True)
    )

    existing_text = local_path.read_text(encoding="utf-8") if local_path.exists() else ""
    base_text = (
        _strip_workspace_sections(existing_text) if payload.force else existing_text.rstrip()
    )
    block = _migration_block(entries)
    updated_text = base_text.rstrip() + "\n\n" + block if base_text else block
    atomic_write_text(local_path, updated_text)

    warnings = (
        ((_disabled_roots_warning(disabled_roots_count),) if disabled_roots_count else ())
        + (_provisional_name_warning(),)
    )
    return WorkspaceMigrateOutput(
        path=local_path.as_posix(),
        migrated_entries=len(entries),
        entries=entries,
        warnings=warnings,
    )


workspace_migrate = async_from_sync(workspace_migrate_sync)


__all__ = [
    "MigratedEntry",
    "WorkspaceInitInput",
    "WorkspaceInitOutput",
    "WorkspaceMigrateInput",
    "WorkspaceMigrateOutput",
    "_deduplicate_names",
    "_generate_entry_name",
    "workspace_init",
    "workspace_init_sync",
    "workspace_migrate",
    "workspace_migrate_sync",
]
