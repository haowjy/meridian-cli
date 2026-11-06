"""Read Claude settings that affect projected permissions and network policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.claude_sessions import resolve_claude_config_root
from meridian.lib.launch.launch_types import CompositionWarning
from meridian.lib.launch.text_utils import dedupe_nonempty

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


def _network_widening_reason(payload: dict[str, object]) -> str | None:
    raw_sandbox = payload.get("sandbox")
    if not isinstance(raw_sandbox, dict):
        return None
    raw_network = cast("dict[str, object]", raw_sandbox).get("network")
    if not isinstance(raw_network, dict):
        return None
    network = cast("dict[str, object]", raw_network)
    allowed_domains = network.get("allowedDomains")
    reasons: list[str] = []
    if isinstance(allowed_domains, list) and bool(cast("list[object]", allowed_domains)):
        reasons.append("a non-empty sandbox.network.allowedDomains list")
    if "httpProxyPort" in network:
        reasons.append("sandbox.network.httpProxyPort")
    if "socksProxyPort" in network:
        reasons.append("sandbox.network.socksProxyPort")
    return ", ".join(reasons) or None


def claude_network_config_warnings(
    child_cwd: Path,
    env: Mapping[str, str],
) -> tuple[CompositionWarning, ...]:
    """Warn when child-selected Claude user settings can widen projected network."""

    settings_path = resolve_claude_config_root(env, child_cwd) / "settings.json"
    settings = _read_settings_payload(settings_path)
    if settings.payload is None and settings.unreadable_reason is None:
        return ()
    if settings.payload is None:
        return (
            CompositionWarning(
                code="claude_network_config_unreadable",
                message=(
                    f"Could not inspect Claude network config {settings_path.as_posix()} "
                    f"({settings.unreadable_reason}); it may widen network access beyond "
                    "Meridian's projected policy."
                ),
            ),
        )

    reason = _network_widening_reason(settings.payload)
    if reason is None:
        return ()
    return (
        CompositionWarning(
            code="claude_network_config_may_widen",
            message=(
                f"Claude network config {settings_path.as_posix()} contains {reason} and "
                "can widen network access beyond Meridian's projected policy; the "
                "child-spawn guardrail compares only Meridian's projected policy."
            ),
        ),
    )


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


def claude_settings_block_plugin_hooks(child_cwd: Path, env: Mapping[str, str]) -> bool:
    """Whether a settings file Claude loads stops plugin hooks modules from running.

    ``disableAllHooks`` and the managed-only policies make Claude skip a plugin's hooks
    module, so the first-turn mod would never deliver the starting prompt.
    """

    settings_dir = child_cwd / ".claude"
    sources = (
        resolve_claude_config_root(env, child_cwd) / "settings.json",
        settings_dir / "settings.json",
        settings_dir / "settings.local.json",
    )
    for settings_path in dict.fromkeys(sources):
        payload = _read_settings_payload(settings_path).payload
        if payload is not None and (
            payload.get("disableAllHooks") is True
            or payload.get("allowManagedHooksOnly")
            or payload.get("allowManagedModsOnly")
        ):
            return True
    return False


__all__ = [
    "claude_network_config_warnings",
    "claude_settings_block_plugin_hooks",
    "read_claude_settings_permissions",
]
