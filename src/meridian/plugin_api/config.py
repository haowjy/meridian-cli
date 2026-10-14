"""Plugin-facing user config accessors."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from typing import Any, cast

from meridian.lib.config.project_root import resolve_user_config_path


def get_user_config() -> dict[str, Any]:
    """Load the resolved user config, including ``MERIDIAN_CONFIG`` overrides.

    Returns an empty mapping when the default config file is missing.
    Raises ``FileNotFoundError`` when ``MERIDIAN_CONFIG`` names a missing file.
    Raises ``tomllib.TOMLDecodeError`` when config content is invalid.
    """

    config_path = resolve_user_config_path(None)
    if config_path is None:
        return {}
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def get_git_overrides() -> dict[str, dict[str, str]]:
    """Get ``[git."<url>"]`` override tables from user config."""

    config = get_user_config()
    git_table = config.get("git")
    if not isinstance(git_table, dict):
        return {}

    overrides: dict[str, dict[str, str]] = {}
    raw_git_table = cast("dict[str, object]", git_table)
    for repo_url, override_value in raw_git_table.items():
        if not isinstance(override_value, Mapping):
            continue
        normalized_override: dict[str, str] = {}
        raw_override = cast("Mapping[object, object]", override_value)
        for key, value in raw_override.items():
            if isinstance(key, str) and isinstance(value, str):
                normalized_override[key] = value
        overrides[repo_url] = normalized_override
    return overrides
