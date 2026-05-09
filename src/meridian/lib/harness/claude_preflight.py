"""Claude-only preflight helpers owned by the Claude adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.launch.launch_types import PreflightResult
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.platform import IS_WINDOWS, get_home_path

logger = structlog.get_logger(__name__)

# Internal sentinel consumed by Claude projection; never forwarded to the CLI.
CLAUDE_PARENT_ALLOWED_TOOLS_FLAG = "--meridian-parent-allowed-tools"


def _default_canonical_claude_config_root() -> Path:
    """Canonical Claude config root when no explicit config env is set."""

    return get_home_path() / ".claude"


def _claude_config_root() -> Path:
    """Resolve the user's real Claude config root."""

    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_canonical_claude_config_root()


def project_slug(project_root: Path) -> str:
    """Map a repo path to Claude's on-disk project slug format."""

    return re.sub(r"[^a-zA-Z0-9]", "-", str(project_root.resolve()))


def _dedupe_roots(*roots: Path | None) -> tuple[Path, ...]:
    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root is None:
            continue
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_roots.append(root)
    return tuple(unique_roots)


def _resolve_source_session_file(
    *,
    source_session_id: str,
    source_slug: str,
    source_roots: tuple[Path, ...],
) -> tuple[Path | None, Path | None]:
    for root in source_roots:
        candidate = root / "projects" / source_slug / f"{source_session_id}.jsonl"
        if candidate.exists():
            return candidate, root
    return None, None


def ensure_claude_session_accessible(
    source_session_id: str,
    source_cwd: Path | None,
    child_cwd: Path,
    *,
    source_config_root: Path | None = None,
    target_config_root: Path | None = None,
) -> None:
    """Make one source Claude session file accessible in the child's project dir.

    On POSIX, creates a symlink. On Windows, copies the file since symlinks
    require developer mode or admin privileges.
    """

    if source_cwd is None:
        return

    source_canonical_root = _claude_config_root()
    resolved_source_config_root = source_config_root or source_canonical_root
    resolved_target_config_root = target_config_root or _claude_config_root()

    same_cwd = source_cwd.resolve() == child_cwd.resolve()
    same_config_root = (
        resolved_source_config_root.resolve() == resolved_target_config_root.resolve()
    )
    if same_cwd and same_config_root:
        return

    # Validate session ID to prevent path traversal.
    safe_session_id = Path(source_session_id).name
    if (
        safe_session_id != source_session_id
        or "/" in source_session_id
        or ".." in source_session_id
    ):
        return

    source_slug = project_slug(source_cwd)
    child_slug = project_slug(child_cwd)

    source_file, source_root_for_copy = _resolve_source_session_file(
        source_session_id=safe_session_id,
        source_slug=source_slug,
        source_roots=_dedupe_roots(source_config_root, source_canonical_root),
    )
    if source_file is None or source_root_for_copy is None:
        return

    target_projects = resolved_target_config_root / "projects"
    child_project = target_projects / child_slug
    child_project.mkdir(parents=True, exist_ok=True)
    target_file = child_project / f"{safe_session_id}.jsonl"

    crosses_config_roots = source_root_for_copy.resolve() != resolved_target_config_root.resolve()

    if IS_WINDOWS or crosses_config_roots:
        # Windows symlinks require developer mode or admin; copy instead
        try:
            if not target_file.exists():
                shutil.copy2(source_file, target_file)
            elif not target_file.samefile(source_file):
                target_file.unlink()
                shutil.copy2(source_file, target_file)
        except OSError:
            pass
        return

    # POSIX: use symlinks
    try:
        os.symlink(source_file, target_file)
    except FileExistsError:
        try:
            if target_file.resolve() != source_file.resolve():
                target_file.unlink()
                os.symlink(source_file, target_file)
        except OSError:
            pass


def read_parent_claude_permissions(execution_cwd: Path) -> tuple[list[str], list[str]]:
    """Read parent Claude settings and return add-dir + allowed-tools payloads."""

    additional_directories: list[str] = []
    allowed_tools: list[str] = []

    settings_dir = execution_cwd / ".claude"
    settings_files = (
        settings_dir / "settings.json",
        settings_dir / "settings.local.json",
    )

    for settings_path in settings_files:
        if not settings_path.exists():
            continue

        try:
            raw_payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Failed to parse parent Claude settings while forwarding child permissions",
                path=str(settings_path),
            )
            continue

        if not isinstance(raw_payload, dict):
            continue
        payload = cast("dict[str, object]", raw_payload)
        raw_permissions = payload.get("permissions")
        if not isinstance(raw_permissions, dict):
            continue
        permissions = cast("dict[str, object]", raw_permissions)

        raw_additional_directories = permissions.get("additionalDirectories")
        if isinstance(raw_additional_directories, list):
            for directory in cast("list[object]", raw_additional_directories):
                if isinstance(directory, str):
                    additional_directories.append(directory)

        raw_allowed_tools = permissions.get("allow")
        if isinstance(raw_allowed_tools, list):
            for tool in cast("list[object]", raw_allowed_tools):
                if isinstance(tool, str):
                    allowed_tools.append(tool)

    return dedupe_nonempty(additional_directories), dedupe_nonempty(allowed_tools)


def expand_claude_passthrough_args(
    *,
    execution_cwd: Path,
    child_cwd: Path,
    passthrough_args: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply Claude-specific passthrough expansion for child execution."""

    if child_cwd.resolve() == execution_cwd.resolve():
        return passthrough_args

    expanded_args: list[str] = [*passthrough_args, "--add-dir", execution_cwd.as_posix()]
    _parent_additional_directories, parent_allowed_tools = read_parent_claude_permissions(
        execution_cwd
    )

    # NOTE: parent additionalDirectories are intentionally not forwarded as
    # passthrough --add-dir entries. Workspace roots flow via projected_roots.

    if parent_allowed_tools:
        expanded_args.extend(
            (
                CLAUDE_PARENT_ALLOWED_TOOLS_FLAG,
                ",".join(parent_allowed_tools),
            )
        )

    return tuple(expanded_args)


def build_claude_preflight_result(
    *,
    execution_cwd: Path,
    child_cwd: Path,
    passthrough_args: tuple[str, ...],
    extra_env: Mapping[str, str] | None = None,
) -> PreflightResult:
    """Build Claude preflight output with immutable env overrides."""

    return PreflightResult.build(
        expanded_passthrough_args=expand_claude_passthrough_args(
            execution_cwd=execution_cwd,
            child_cwd=child_cwd,
            passthrough_args=passthrough_args,
        ),
        extra_env=dict(extra_env or {}),
    )


__all__ = [
    "CLAUDE_PARENT_ALLOWED_TOOLS_FLAG",
    "build_claude_preflight_result",
    "ensure_claude_session_accessible",
    "expand_claude_passthrough_args",
    "project_slug",
    "read_parent_claude_permissions",
]
