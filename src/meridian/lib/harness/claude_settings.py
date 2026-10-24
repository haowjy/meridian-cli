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


def read_claude_settings_permissions(
    child_cwd: Path, env: Mapping[str, str]
) -> tuple[
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    """Return permission allows and user-scope network domains Claude loads itself.

    Claude merges the user, project and local settings files into every launch; Meridian
    passes no ``--setting-sources``, so their tool allows can widen a profile policy. Only
    user-scope network domains widened the launch-local strict allowlist in the live probe;
    project files remain tool-permission sources but not network-policy inputs.
    """

    settings_dir = child_cwd / ".claude"
    user_settings_path = resolve_claude_config_root(env, child_cwd) / "settings.json"
    sources = (
        user_settings_path,
        settings_dir / "settings.json",
        settings_dir / "settings.local.json",
    )
    permission_allows: list[tuple[str, tuple[str, ...]]] = []
    network_allowed_domains: list[tuple[str, tuple[str, ...]]] = []
    network_denied_domains: list[tuple[str, tuple[str, ...]]] = []
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
        payload = cast("dict[str, object]", raw_payload)
        raw_permissions = payload.get("permissions")
        if isinstance(raw_permissions, dict):
            raw_allow = cast("dict[str, object]", raw_permissions).get("allow")
            if isinstance(raw_allow, list):
                allows = dedupe_nonempty(
                    entry for entry in cast("list[object]", raw_allow) if isinstance(entry, str)
                )
                if allows:
                    permission_allows.append((settings_path.as_posix(), tuple(allows)))

        if settings_path != user_settings_path:
            continue
        raw_sandbox = payload.get("sandbox")
        if not isinstance(raw_sandbox, dict):
            continue
        raw_network = cast("dict[str, object]", raw_sandbox).get("network")
        if not isinstance(raw_network, dict):
            continue
        network = cast("dict[str, object]", raw_network)
        for key, destination in (
            ("allowedDomains", network_allowed_domains),
            ("deniedDomains", network_denied_domains),
        ):
            raw_domains = network.get(key)
            if not isinstance(raw_domains, list):
                continue
            domains = dedupe_nonempty(
                entry for entry in cast("list[object]", raw_domains) if isinstance(entry, str)
            )
            if domains:
                destination.append((settings_path.as_posix(), tuple(domains)))
    return (
        tuple(permission_allows),
        tuple(network_allowed_domains),
        tuple(network_denied_domains),
    )


__all__ = ["read_claude_settings_permissions"]
