"""Read Claude settings that affect projected permissions and network policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.claude_sessions import resolve_claude_config_root
from meridian.lib.launch.text_utils import dedupe_nonempty

logger = structlog.get_logger(__name__)


def _read_settings_payload(settings_path: Path) -> dict[str, object] | None:
    if not settings_path.is_file():
        return None
    try:
        raw_payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Failed to parse Claude settings",
            path=str(settings_path),
        )
        return None
    if not isinstance(raw_payload, dict):
        return None
    return cast("dict[str, object]", raw_payload)


def _network_domains(
    payload: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw_sandbox = payload.get("sandbox")
    if not isinstance(raw_sandbox, dict):
        return (), ()
    raw_network = cast("dict[str, object]", raw_sandbox).get("network")
    if not isinstance(raw_network, dict):
        return (), ()
    network = cast("dict[str, object]", raw_network)

    def domains(key: str) -> tuple[str, ...]:
        raw_domains = network.get(key)
        if not isinstance(raw_domains, list):
            return ()
        return tuple(
            dedupe_nonempty(
                entry for entry in cast("list[object]", raw_domains) if isinstance(entry, str)
            )
        )

    return (
        domains("allowedDomains"),
        domains("deniedDomains"),
    )


def read_claude_user_network_domains(
    env: Mapping[str, str],
) -> tuple[
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    """Return network domains from the user settings root selected by the child env."""

    child_cwd = Path(env.get("MERIDIAN_PROJECT_DIR", "") or Path.cwd())
    settings_path = resolve_claude_config_root(env, child_cwd) / "settings.json"
    payload = _read_settings_payload(settings_path)
    if payload is None:
        return (), ()
    allowed, denied = _network_domains(payload)
    return (
        ((settings_path.as_posix(), allowed),) if allowed else (),
        ((settings_path.as_posix(), denied),) if denied else (),
    )


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
        payload = _read_settings_payload(settings_path)
        if payload is None:
            continue
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
        allowed, denied = _network_domains(payload)
        if allowed:
            network_allowed_domains.append((settings_path.as_posix(), allowed))
        if denied:
            network_denied_domains.append((settings_path.as_posix(), denied))
    return (
        tuple(permission_allows),
        tuple(network_allowed_domains),
        tuple(network_denied_domains),
    )


__all__ = ["read_claude_settings_permissions", "read_claude_user_network_domains"]
