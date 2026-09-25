"""Claude-only preflight helpers owned by the Claude adapter."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

import structlog

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeKeyFields,
    NativeSessionUnavailable,
)
from meridian.lib.harness.claude_sessions import project_slug, resolve_claude_config_root
from meridian.lib.launch.launch_types import PreflightResult
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace

logger = structlog.get_logger(__name__)

# Internal sentinel consumed by Claude projection; never forwarded to the CLI.
CLAUDE_PARENT_ALLOWED_TOOLS_FLAG = "--meridian-parent-allowed-tools"


def _claude_config_root() -> Path:
    return resolve_claude_config_root(os.environ, Path.cwd())


def validate_claude_session_file(path: Path, session_id: str) -> None:
    """Verify the exact native source, not merely its filename."""
    try:
        with path.open(encoding="utf-8") as handle:
            header = json.loads(handle.readline())
    except (OSError, UnicodeError, ValueError) as exc:
        raise NativeSessionUnavailable(session_id, "missing") from exc
    observed = header.get("sessionId") if isinstance(header, dict) else None
    if not isinstance(observed, str) or not observed:
        raise NativeSessionUnavailable(session_id, "missing")
    if observed != session_id:
        raise NativeEntryMismatch(
            NativeKeyFields("claude", str(path.parent), session_id),
            NativeKeyFields("claude", str(path.parent), str(observed)),
        )


def ensure_claude_session_accessible(
    source_session_id: str,
    child_cwd: Path,
    *,
    source_native_store: Path,
    target_config_root: Path | None = None,
) -> None:
    """Seed the child's project from exactly the recorded store/ID pair."""
    source_file = source_native_store / f"{source_session_id}.jsonl"
    validate_claude_session_file(source_file, source_session_id)
    target_root = target_config_root or _claude_config_root()
    target_file = target_root / "projects" / project_slug(child_cwd) / source_file.name
    if target_file.exists() and target_file.samefile(source_file):
        return
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS or source_native_store.parent.parent.resolve() != target_root.resolve():
        with source_file.open("rb") as source, atomic_replace(target_file, mode="wb") as target:
            shutil.copyfileobj(source, target)
    else:
        temporary = target_file.with_name(f".{target_file.name}.{uuid4().hex}.tmp")
        try:
            temporary.symlink_to(source_file)
            os.replace(temporary, target_file)
        finally:
            temporary.unlink(missing_ok=True)


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
