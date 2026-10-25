"""Detect Codex config that can widen projected network policy."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.launch.launch_types import CompositionWarning

logger = structlog.get_logger(__name__)


def _unreadable(path: Path, reason: str) -> CompositionWarning:
    logger.warning("Failed to parse Codex config", path=str(path), reason=reason)
    return CompositionWarning(
        code="codex_network_config_unreadable",
        message=(
            f"Could not inspect Codex network config {path.as_posix()} ({reason}); it may "
            "widen network access beyond Meridian's projected policy."
        ),
    )


def _network_config_warning(config_path: Path) -> CompositionWarning | None:
    try:
        with config_path.open("rb") as config_file:
            payload = cast("dict[str, object]", tomllib.load(config_file))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _unreadable(config_path, f"{type(exc).__name__}: {exc}")
    except tomllib.TOMLDecodeError:
        return _unreadable(config_path, "invalid TOML")

    raw_features = payload.get("features")
    if raw_features is None:
        return None
    if not isinstance(raw_features, dict):
        return _unreadable(config_path, "features is not a table")
    raw_proxy = cast("dict[str, object]", raw_features).get("network_proxy")
    if raw_proxy is None:
        return None
    if isinstance(raw_proxy, bool):
        # The bool form enables or disables the proxy feature but declares no domains.
        return None
    if not isinstance(raw_proxy, dict):
        return _unreadable(config_path, "features.network_proxy is not a table")
    raw_domains = cast("dict[str, object]", raw_proxy).get("domains")
    if raw_domains is None:
        return None
    if not isinstance(raw_domains, dict):
        return _unreadable(
            config_path,
            "features.network_proxy.domains is not a table",
        )

    widens = False
    for raw_host, raw_action in cast("dict[object, object]", raw_domains).items():
        if not isinstance(raw_host, str) or not raw_host.strip():
            return _unreadable(config_path, "network proxy domain host is empty")
        if raw_action == "allow":
            widens = True
        elif raw_action != "deny":
            return _unreadable(
                config_path,
                f"network proxy domain {raw_host!r} is not allow or deny",
            )

    if not widens:
        return None
    return CompositionWarning(
        code="codex_network_config_may_widen",
        message=(
            f"Codex network config {config_path.as_posix()} contains an allow entry under "
            "features.network_proxy.domains and can widen network access beyond Meridian's "
            "projected policy; the child-spawn guardrail compares only Meridian's projected "
            "policy."
        ),
    )


def codex_network_config_warnings(
    child_cwd: Path,
    env: Mapping[str, str],
) -> tuple[CompositionWarning, ...]:
    """Warn about widening in child-selected user and ancestor Codex configs."""

    codex_home = resolve_codex_home(env)
    if not codex_home.is_absolute():
        codex_home = child_cwd / codex_home
    paths = [
        codex_home / "config.toml",
        *(directory / ".codex" / "config.toml" for directory in (child_cwd, *child_cwd.parents)),
    ]
    warnings: list[CompositionWarning] = []
    for path in dict.fromkeys(paths):
        warning = _network_config_warning(path)
        if warning is not None:
            warnings.append(warning)
    return tuple(warnings)


__all__ = ["codex_network_config_warnings"]
