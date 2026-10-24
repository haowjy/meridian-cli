"""Read Claude settings that affect projected permissions and network policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.claude_sessions import resolve_claude_config_root
from meridian.lib.launch.text_utils import dedupe_nonempty
from meridian.lib.safety.policy import HarnessNetworkConfig

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _SettingsRead:
    payload: dict[str, object] | None = None
    unreadable_reason: str | None = None


def _read_settings_payload(settings_path: Path) -> _SettingsRead:
    try:
        with settings_path.open(encoding="utf-8") as settings_file:
            raw_payload = json.load(settings_file)
    except FileNotFoundError:
        return _SettingsRead()
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    except json.JSONDecodeError:
        reason = "invalid JSON"
    else:
        if isinstance(raw_payload, dict):
            return _SettingsRead(payload=cast("dict[str, object]", raw_payload))
        reason = "top level is not an object"

    logger.warning(
        "Failed to parse Claude settings",
        path=str(settings_path),
        reason=reason,
    )
    return _SettingsRead(unreadable_reason=reason)


def _network_widens(payload: dict[str, object]) -> bool:
    raw_sandbox = payload.get("sandbox")
    if not isinstance(raw_sandbox, dict):
        return False
    raw_network = cast("dict[str, object]", raw_sandbox).get("network")
    if not isinstance(raw_network, dict):
        return False
    network = cast("dict[str, object]", raw_network)
    allowed_domains = network.get("allowedDomains")
    return (
        (
            isinstance(allowed_domains, list)
            and bool(cast("list[object]", allowed_domains))
        )
        or "httpProxyPort" in network
        or "socksProxyPort" in network
    )


def read_claude_user_network_config(
    env: Mapping[str, str],
) -> HarnessNetworkConfig | None:
    """Return network facts from the user settings root selected by the child env."""

    child_cwd = Path(env.get("MERIDIAN_PROJECT_DIR", "") or Path.cwd())
    settings_path = resolve_claude_config_root(env, child_cwd) / "settings.json"
    settings = _read_settings_payload(settings_path)
    if settings.payload is None and settings.unreadable_reason is None:
        return None
    if settings.payload is None:
        return HarnessNetworkConfig(
            path=settings_path.as_posix(),
            unreadable_reason=settings.unreadable_reason,
        )

    if not _network_widens(settings.payload):
        return None
    return HarnessNetworkConfig(path=settings_path.as_posix(), bypasses_enforcement=True)


def read_claude_settings_permissions(
    child_cwd: Path, env: Mapping[str, str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return permission allows from settings files Claude loads itself.

    Claude merges the user, project and local settings files into every launch; Meridian
    passes no ``--setting-sources``, so their tool allows can widen a profile policy. The
    network-facts binder separately owns user network settings and their attribution.
    """

    settings_dir = child_cwd / ".claude"
    user_settings_path = resolve_claude_config_root(env, child_cwd) / "settings.json"
    sources = (
        user_settings_path,
        settings_dir / "settings.json",
        settings_dir / "settings.local.json",
    )
    permission_allows: list[tuple[str, tuple[str, ...]]] = []
    for settings_path in dict.fromkeys(sources):
        payload = _read_settings_payload(settings_path).payload
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
    return tuple(permission_allows)


__all__ = ["read_claude_settings_permissions", "read_claude_user_network_config"]
