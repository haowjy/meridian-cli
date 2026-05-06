"""Claude-only preflight helpers owned by the Claude adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import structlog

from meridian.lib.launch.launch_types import PreflightResult
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.platform import IS_WINDOWS, get_home_path
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text

logger = structlog.get_logger(__name__)

# Internal sentinel consumed by Claude projection; never forwarded to the CLI.
CLAUDE_PARENT_ALLOWED_TOOLS_FLAG = "--meridian-parent-allowed-tools"
MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV = "MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR"
_SKIP_ENTRIES = frozenset({"projects"})
_COPY_ENTRIES = frozenset(
    {".claude.json", ".credentials.json", "statsig", "memory", "cached_preferences", "todos"}
)
_OVERLAY_METADATA_FILENAME = ".meridian-overlay.json"
_TRANSCRIPT_LOCKS_DIRNAME = ".meridian-transcript-locks"
_AUTH_STATE_FILENAMES = (".claude.json", ".credentials.json")


def _safe_log(level: str, event: str, **kwargs: object) -> None:
    """Best-effort logging for warning-only overlay failure paths."""

    with suppress(Exception):
        getattr(logger, level)(event, **kwargs)


@dataclass(frozen=True)
class ClaudeOverlayRoots:
    """Resolved Claude config roots for one overlay lifecycle."""

    effective_config_root: Path
    materialization_root: Path


@dataclass(frozen=True)
class ClaudeOverlayCleanupResult:
    """Result of best-effort Claude overlay cleanup."""

    materialization_root: Path | None
    removed: bool
    materialization: ClaudeOverlayMaterializationResult | None

    @property
    def materialized(self) -> bool:
        """Backward-compatible success flag for transcript materialization."""

        return self.materialization is not None and self.materialization.succeeded


@dataclass(frozen=True)
class ClaudeOverlayMaterializationResult:
    """Structured transcript materialization outcome for one overlay."""

    materialization_root: Path
    discovered_transcripts: int
    copied_transcripts: int
    failed_transcripts: int

    @property
    def succeeded(self) -> bool:
        """Whether every discovered transcript was handled without failure."""

        return self.failed_transcripts == 0


def _default_canonical_claude_config_root() -> Path:
    """Canonical Claude config root when no explicit config env is set."""

    return get_home_path() / ".claude"


def _claude_config_root() -> Path:
    """Resolve the user's real Claude config root."""

    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_canonical_claude_config_root()


def _overlay_source_root_and_original_env() -> tuple[Path, str]:
    """Resolve overlay source root + durable-root metadata payload.

    Returns:
        (source_root, original_config_env_payload)
    """

    original_config = os.environ.get(MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV)
    if original_config is not None:
        stripped = original_config.strip()
        if stripped:
            return Path(stripped).expanduser(), stripped
        return _default_canonical_claude_config_root(), ""

    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser(), configured
    return _default_canonical_claude_config_root(), ""


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


def resolve_claude_overlay_roots(
    *,
    isolated_config_root: Path | None,
    original_config_env: str,
) -> ClaudeOverlayRoots:
    """Resolve effective runtime root plus durable materialization root."""

    materialization_root = resolve_overlay_materialization_canonical_root(
        {MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV: original_config_env}
    )
    if isolated_config_root is not None:
        effective_config_root = isolated_config_root
    else:
        fallback_config_dir = original_config_env.strip()
        effective_config_root = (
            Path(fallback_config_dir) if fallback_config_dir else materialization_root
        )
    return ClaudeOverlayRoots(
        effective_config_root=effective_config_root,
        materialization_root=materialization_root,
    )


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


def _overlay_metadata_path(overlay_root: Path) -> Path:
    return overlay_root / _OVERLAY_METADATA_FILENAME


def _write_overlay_metadata(
    *,
    overlay_root: Path,
    materialization_root: Path,
) -> None:
    atomic_write_text(
        _overlay_metadata_path(overlay_root),
        json.dumps(
            {
                "v": 1,
                "materialization_root": materialization_root.as_posix(),
            },
            indent=2,
        )
        + "\n",
    )


def _read_overlay_materialization_root(overlay_root: Path) -> Path | None:
    metadata_path = _overlay_metadata_path(overlay_root)
    try:
        raw_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        _safe_log(
            "warning",
            "Failed to parse Claude overlay metadata; falling back to ambient durable root",
            overlay_metadata_path=str(metadata_path),
            exc_info=True,
        )
        return None

    if not isinstance(raw_payload, dict):
        _safe_log(
            "warning",
            "Malformed Claude overlay metadata payload; falling back to ambient durable root",
            overlay_metadata_path=str(metadata_path),
        )
        return None

    payload = cast("dict[str, object]", raw_payload)
    materialization_root = payload.get("materialization_root")
    if not isinstance(materialization_root, str):
        _safe_log(
            "warning",
            (
                "Claude overlay metadata missing materialization_root; "
                "falling back to ambient durable root"
            ),
            overlay_metadata_path=str(metadata_path),
        )
        return None

    stripped = materialization_root.strip()
    if not stripped:
        _safe_log(
            "warning",
            (
                "Claude overlay metadata had empty materialization_root; "
                "falling back to ambient durable root"
            ),
            overlay_metadata_path=str(metadata_path),
        )
        return None

    return Path(stripped).expanduser()


def _overlay_transcript_lock_path(
    *,
    canonical_root: Path,
    relative_path: Path,
) -> Path:
    lock_key = sha256(relative_path.as_posix().encode("utf-8")).hexdigest()
    return canonical_root / _TRANSCRIPT_LOCKS_DIRNAME / f"{lock_key}.lock"


def _overlay_transcript_wins(overlay_stat: os.stat_result, target_stat: os.stat_result) -> bool:
    return overlay_stat.st_mtime > target_stat.st_mtime or (
        overlay_stat.st_mtime == target_stat.st_mtime and overlay_stat.st_size > target_stat.st_size
    )


def materialize_overlay_auth_state(
    overlay_root: Path,
    canonical_root: Path | None = None,
) -> None:
    """Copy mutable Claude auth state from one overlay into the durable root."""

    resolved_canonical_root = canonical_root or _claude_config_root()
    for filename in _AUTH_STATE_FILENAMES:
        overlay_file = overlay_root / filename
        if not overlay_file.is_file():
            continue

        target = resolved_canonical_root / filename
        temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            resolved_canonical_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            _safe_log(
                "warning",
                "Failed to create canonical Claude auth directory",
                canonical_root=str(resolved_canonical_root),
                exc_info=True,
            )
            continue

        try:
            overlay_stat = overlay_file.stat()
            shutil.copy2(overlay_file, temp_target)
            lock_path = _overlay_transcript_lock_path(
                canonical_root=resolved_canonical_root,
                relative_path=Path("_auth") / filename,
            )
            with lock_file(lock_path):
                should_copy = True
                if target.exists():
                    target_stat = target.stat()
                    should_copy = _overlay_transcript_wins(overlay_stat, target_stat)
                if should_copy:
                    os.replace(temp_target, target)
                else:
                    temp_target.unlink(missing_ok=True)
        except OSError:
            _safe_log(
                "warning",
                "Failed to materialize Claude overlay auth state",
                overlay_file=str(overlay_file),
                canonical_file=str(target),
                exc_info=True,
            )
            with suppress(OSError):
                temp_target.unlink(missing_ok=True)


def prepare_isolated_claude_config(
    runtime_root: Path,
    spawn_id: str,
) -> tuple[Path | None, str]:
    """Create an overlay config dir isolating Claude's projects/ subtree.

    Returns the isolated root, or ``None`` when setup fails, plus the original
    ``CLAUDE_CONFIG_DIR`` environment value (empty string when unset).
    """

    user_root, original_env = _overlay_source_root_and_original_env()
    isolated_root = runtime_root / "claude-config" / spawn_id

    try:
        isolated_root.mkdir(parents=True, exist_ok=True)
        _write_overlay_metadata(
            overlay_root=isolated_root,
            materialization_root=resolve_overlay_materialization_canonical_root(
                {MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV: original_env}
            ),
        )

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
        _safe_log(
            "warning",
            "Failed to create isolated Claude config; proceeding without isolation",
            exc_info=True,
        )
        return None, original_env

    return isolated_root, original_env


def materialize_overlay_transcripts(
    overlay_root: Path,
    canonical_root: Path | None = None,
) -> ClaudeOverlayMaterializationResult:
    """Copy overlay session transcripts into the canonical Claude config root."""

    resolved_canonical_root = canonical_root or _claude_config_root()
    overlay_projects = overlay_root / "projects"
    if not overlay_projects.is_dir():
        return ClaudeOverlayMaterializationResult(
            materialization_root=resolved_canonical_root,
            discovered_transcripts=0,
            copied_transcripts=0,
            failed_transcripts=0,
        )

    canonical_projects = resolved_canonical_root / "projects"
    copied_transcripts = 0
    failed_transcripts = 0

    try:
        session_files = [path for path in overlay_projects.rglob("*.jsonl") if path.is_file()]
    except OSError:
        _safe_log(
            "warning",
            "Failed to list overlay projects directory",
            overlay_projects=str(overlay_projects),
            exc_info=True,
        )
        return ClaudeOverlayMaterializationResult(
            materialization_root=resolved_canonical_root,
            discovered_transcripts=0,
            copied_transcripts=0,
            failed_transcripts=1,
        )

    for session_file in session_files:
        relative_path = session_file.relative_to(overlay_projects)
        target = canonical_projects / relative_path
        temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            failed_transcripts += 1
            _safe_log(
                "warning",
                "Failed to create canonical Claude transcript directory",
                canonical_project_dir=str(target.parent),
                exc_info=True,
            )
            continue

        try:
            overlay_stat = session_file.stat()
            shutil.copy2(session_file, temp_target)
            lock_path = _overlay_transcript_lock_path(
                canonical_root=resolved_canonical_root,
                relative_path=relative_path,
            )
            with lock_file(lock_path):
                should_copy = True
                if target.exists():
                    target_stat = target.stat()
                    should_copy = _overlay_transcript_wins(overlay_stat, target_stat)
                if should_copy:
                    os.replace(temp_target, target)
                    copied_transcripts += 1
                else:
                    temp_target.unlink(missing_ok=True)
        except OSError:
            failed_transcripts += 1
            _safe_log(
                "warning",
                "Failed to materialize Claude overlay transcript",
                overlay_transcript=str(session_file),
                canonical_transcript=str(target),
                exc_info=True,
            )
            with suppress(OSError):
                temp_target.unlink(missing_ok=True)

    return ClaudeOverlayMaterializationResult(
        materialization_root=resolved_canonical_root,
        discovered_transcripts=len(session_files),
        copied_transcripts=copied_transcripts,
        failed_transcripts=failed_transcripts,
    )


def cleanup_claude_overlay(
    overlay_root: Path | None,
    *,
    canonical_root: Path | None = None,
    remove_overlay: Callable[[Path], bool] | None = None,
) -> ClaudeOverlayCleanupResult:
    """Materialize transcripts, then best-effort remove one Claude overlay."""

    if overlay_root is None or not overlay_root.is_dir():
        return ClaudeOverlayCleanupResult(
            materialization_root=None,
            removed=False,
            materialization=None,
        )

    resolved_canonical_root = (
        canonical_root
        if canonical_root is not None
        else _read_overlay_materialization_root(overlay_root)
        or resolve_overlay_materialization_canonical_root()
    )
    materialization: ClaudeOverlayMaterializationResult | None = None
    try:
        materialize_overlay_auth_state(
            overlay_root,
            canonical_root=resolved_canonical_root,
        )
    except Exception:
        _safe_log(
            "warning",
            "Claude overlay auth-state materialization failed; auth/session continuity may degrade",
            overlay_root=str(overlay_root),
            canonical_root=str(resolved_canonical_root),
            exc_info=True,
        )

    try:
        materialization = materialize_overlay_transcripts(
            overlay_root,
            canonical_root=resolved_canonical_root,
        )
    except Exception:
        _safe_log(
            "warning",
            "Claude overlay transcript materialization failed; session transcripts may be lost",
            overlay_root=str(overlay_root),
            canonical_root=str(resolved_canonical_root),
            exc_info=True,
        )

    removed = False
    if remove_overlay is None:
        try:
            shutil.rmtree(overlay_root)
            removed = True
        except OSError:
            _safe_log(
                "debug",
                "Failed to clean up isolated Claude config dir",
                overlay_root=str(overlay_root),
                exc_info=True,
            )
    else:
        try:
            removed = remove_overlay(overlay_root)
        except Exception:
            _safe_log(
                "debug",
                "Failed to remove Claude overlay via custom remover",
                overlay_root=str(overlay_root),
                exc_info=True,
            )

    return ClaudeOverlayCleanupResult(
        materialization_root=resolved_canonical_root,
        removed=removed,
        materialization=materialization,
    )


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

    source_canonical_root = resolve_overlay_materialization_canonical_root()
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
    "MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV",
    "ClaudeOverlayCleanupResult",
    "ClaudeOverlayMaterializationResult",
    "ClaudeOverlayRoots",
    "build_claude_preflight_result",
    "cleanup_claude_overlay",
    "ensure_claude_session_accessible",
    "expand_claude_passthrough_args",
    "materialize_overlay_transcripts",
    "prepare_isolated_claude_config",
    "project_slug",
    "read_parent_claude_permissions",
    "resolve_claude_overlay_roots",
    "resolve_overlay_materialization_canonical_root",
]
