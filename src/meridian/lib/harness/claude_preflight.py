"""Claude-only preflight helpers owned by the Claude adapter."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

import structlog

from meridian.lib.harness.claude_sessions import (
    project_slug,
    resolve_claude_config_root,
    validate_claude_session_file,
)
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace

logger = structlog.get_logger(__name__)

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
    target_root = target_config_root or resolve_claude_config_root(os.environ, Path.cwd())
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


def read_claude_settings_allows(
    child_cwd: Path, env: Mapping[str, str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the ``permissions.allow`` entries of each settings file Claude loads itself.

    Claude merges the user, project and local settings files into every launch; Meridian
    passes no ``--setting-sources``, so their allows widen a profile allowlist. The
    permission compile reports them; it cannot remove them.
    """

    settings_dir = child_cwd / ".claude"
    sources = (
        resolve_claude_config_root(env, child_cwd) / "settings.json",
        settings_dir / "settings.json",
        settings_dir / "settings.local.json",
    )
    found: list[tuple[str, tuple[str, ...]]] = []
    for settings_path in dict.fromkeys(sources):
        if not settings_path.is_file():
            continue
        try:
            raw_payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Failed to parse Claude settings while reporting permissions",
                path=str(settings_path),
            )
            continue
        if not isinstance(raw_payload, dict):
            continue
        raw_permissions = cast("dict[str, object]", raw_payload).get("permissions")
        if not isinstance(raw_permissions, dict):
            continue
        raw_allow = cast("dict[str, object]", raw_permissions).get("allow")
        if not isinstance(raw_allow, list):
            continue
        allows = dedupe_nonempty(
            entry for entry in cast("list[object]", raw_allow) if isinstance(entry, str)
        )
        if allows:
            found.append((settings_path.as_posix(), tuple(allows)))
    return tuple(found)


__all__ = [
    "ensure_claude_session_accessible",
    "project_slug",
    "read_claude_settings_allows",
]
