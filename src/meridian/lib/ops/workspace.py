"""Workspace file operations."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync, resolve_project_authority
from meridian.lib.platform.atomic import atomic_write_text

_WORKSPACE_TEMPLATE = """# Workspace topology — local path overrides and additions.
# Override committed [workspace] paths for your local checkout.
#
# [workspace.example]
# path = "../sibling-repo"
"""

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


def _is_linked_worktree_git_dir(git_dir: Path) -> bool:
    return "worktrees" in git_dir.parts


def _ensure_local_gitignore_entries(
    *,
    project_root: Path,
    entries: tuple[str, ...],
) -> tuple[Path | None, bool]:
    git_dir = _resolve_git_dir(project_root)
    if git_dir is None:
        return None, False
    if _is_linked_worktree_git_dir(git_dir):
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


__all__ = [
    "WorkspaceInitInput",
    "WorkspaceInitOutput",
    "workspace_init",
    "workspace_init_sync",
]
