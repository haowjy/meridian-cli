"""Claude settings-file permission reporting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.claude_sessions import resolve_claude_config_root
from meridian.lib.launch.text_utils import dedupe_nonempty

logger = structlog.get_logger(__name__)


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
