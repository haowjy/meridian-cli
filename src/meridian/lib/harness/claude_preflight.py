"""Claude-only preflight helpers owned by the Claude adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast

import structlog

from meridian.lib.launch.launch_types import PreflightResult
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.platform import IS_WINDOWS, get_home_path

logger = structlog.get_logger(__name__)

# Internal sentinel consumed by Claude projection; never forwarded to the CLI.
CLAUDE_PARENT_ALLOWED_TOOLS_FLAG = "--meridian-parent-allowed-tools"
MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV = "MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR"
_SKIP_ENTRIES = frozenset({"projects"})
_COPY_ENTRIES = frozenset({".claude.json", "statsig", "memory", "cached_preferences", "todos"})


def _default_canonical_claude_config_root() -> Path:
    """Canonical Claude config root when no explicit config env is set."""

    return get_home_path() / ".claude"


def _claude_config_root() -> Path:
    """Resolve the user's real Claude config root."""

    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_canonical_claude_config_root()


def resolve_overlay_materialization_canonical_root(
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve durable target root for Claude transcript materialization.

    Resolution order:
    1. Internal Meridian metadata env var (non-empty).
    2. Internal Meridian metadata env var present-but-empty => default canonical root.
    3. Ambient Claude config root (legacy fallback).
    """

    resolved_env = env if env is not None else os.environ
    original_config = resolved_env.get(MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV)
    if original_config is not None:
        original_config = original_config.strip()
        if original_config:
            return Path(original_config).expanduser()
        return _default_canonical_claude_config_root()
    return _claude_config_root()


def _claude_credentials_source(config_root: Path) -> Path | None:
    """Return the best available source for Claude's credentials file."""

    candidates = (
        config_root / ".claude.json",
        get_home_path() / ".claude.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def project_slug(project_root: Path) -> str:
    """Map a repo path to Claude's on-disk project slug format."""

    return re.sub(r"[^a-zA-Z0-9]", "-", str(project_root.resolve()))


def _classify_overlay_entry(name: str) -> Literal["skip", "copy", "link"]:
    """Classify one Claude config overlay entry."""

    if name in _SKIP_ENTRIES:
        return "skip"
    if name in _COPY_ENTRIES:
        return "copy"
    return "link"


def _copy_entry(entry: Path, target: Path) -> None:
    """Copy a mutable entry into the overlay."""

    if entry.is_dir():
        shutil.copytree(entry, target, symlinks=True)
    else:
        shutil.copy2(entry, target)


def _link_entry(entry: Path, target: Path) -> None:
    """Link a read-only entry into the overlay."""

    if IS_WINDOWS:
        if entry.is_dir():
            import _winapi as winapi

            winapi.CreateJunction(str(entry), str(target))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        else:
            shutil.copy2(entry, target)
        return

    os.symlink(entry, target)


def prepare_isolated_claude_config(
    runtime_root: Path,
    spawn_id: str,
) -> tuple[Path | None, str]:
    """Create an overlay config dir isolating Claude's projects/ subtree.

    Returns the isolated root, or ``None`` when setup fails, plus the original
    ``CLAUDE_CONFIG_DIR`` environment value (empty string when unset).
    """

    original_env = os.environ.get("CLAUDE_CONFIG_DIR", "")
    user_root = _claude_config_root()
    isolated_root = runtime_root / "claude-config" / spawn_id

    try:
        isolated_root.mkdir(parents=True, exist_ok=True)

        if user_root.is_dir():
            for entry in user_root.iterdir():
                target = isolated_root / entry.name
                if target.exists() or target.is_symlink():
                    continue
                action = _classify_overlay_entry(entry.name)
                if action == "skip":
                    continue
                if action == "copy":
                    _copy_entry(entry, target)
                else:
                    _link_entry(entry, target)

        (isolated_root / "projects").mkdir(exist_ok=True)

        isolated_credentials = isolated_root / ".claude.json"
        if not isolated_credentials.exists():
            user_credentials = _claude_credentials_source(user_root)
            if user_credentials is not None:
                shutil.copy2(user_credentials, isolated_credentials)
    except OSError:
        logger.warning(
            "Failed to create isolated Claude config; proceeding without isolation",
            exc_info=True,
        )
        return None, original_env

    return isolated_root, original_env


def materialize_overlay_transcripts(
    overlay_root: Path,
    canonical_root: Path | None = None,
) -> int:
    """Copy overlay session transcripts into the canonical Claude config root."""

    overlay_projects = overlay_root / "projects"
    if not overlay_projects.is_dir():
        return 0

    resolved_canonical_root = canonical_root or _claude_config_root()
    canonical_projects = resolved_canonical_root / "projects"
    materialized = 0

    try:
        slug_dirs = list(overlay_projects.iterdir())
    except OSError:
        logger.warning(
            "Failed to list overlay projects directory",
            overlay_projects=str(overlay_projects),
            exc_info=True,
        )
        return 0

    for slug_dir in slug_dirs:
        if not slug_dir.is_dir():
            continue
        try:
            session_files = list(slug_dir.iterdir())
        except OSError:
            logger.warning(
                "Failed to list overlay project transcript directory",
                overlay_project_dir=str(slug_dir),
                exc_info=True,
            )
            continue

        canonical_slug_dir = canonical_projects / slug_dir.name
        for session_file in session_files:
            if not session_file.is_file() or session_file.suffix != ".jsonl":
                continue

            try:
                canonical_slug_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning(
                    "Failed to create canonical Claude transcript directory",
                    canonical_project_dir=str(canonical_slug_dir),
                    exc_info=True,
                )
                continue

            target = canonical_slug_dir / session_file.name
            try:
                should_copy = True
                if target.exists():
                    overlay_stat = session_file.stat()
                    target_stat = target.stat()
                    should_copy = overlay_stat.st_mtime > target_stat.st_mtime or (
                        overlay_stat.st_mtime == target_stat.st_mtime
                        and overlay_stat.st_size > target_stat.st_size
                    )
                if should_copy:
                    temp_target = target.with_name(
                        f".{target.name}.{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        shutil.copy2(session_file, temp_target)
                        os.replace(temp_target, target)
                    except OSError:
                        with suppress(OSError):
                            temp_target.unlink(missing_ok=True)
                        raise
                    materialized += 1
            except OSError:
                logger.warning(
                    "Failed to materialize Claude overlay transcript",
                    overlay_transcript=str(session_file),
                    canonical_transcript=str(target),
                    exc_info=True,
                )

    return materialized


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

    default_config_root = _claude_config_root()
    resolved_source_config_root = source_config_root or default_config_root
    resolved_target_config_root = target_config_root or default_config_root

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

    source_file: Path | None = None
    source_root_for_copy = resolved_source_config_root
    if source_config_root is not None:
        configured_source = (
            source_config_root / "projects" / source_slug / f"{safe_session_id}.jsonl"
        )
        if configured_source.exists():
            source_file = configured_source
            source_root_for_copy = source_config_root

    if source_file is None:
        canonical_source = (
            default_config_root / "projects" / source_slug / f"{safe_session_id}.jsonl"
        )
        if canonical_source.exists():
            source_file = canonical_source
            source_root_for_copy = default_config_root

    if source_file is None:
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
    "MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV",
    "build_claude_preflight_result",
    "ensure_claude_session_accessible",
    "expand_claude_passthrough_args",
    "materialize_overlay_transcripts",
    "prepare_isolated_claude_config",
    "project_slug",
    "read_parent_claude_permissions",
    "resolve_overlay_materialization_canonical_root",
]
