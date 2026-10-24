"""Detect Codex config that can widen projected network policy."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.safety.policy import HarnessNetworkConfig

logger = structlog.get_logger(__name__)


def _unreadable(path: Path, reason: str) -> HarnessNetworkConfig:
    logger.warning("Failed to parse Codex config", path=str(path), reason=reason)
    return HarnessNetworkConfig(path=path.as_posix(), unreadable_reason=reason)


def _read_network_config(config_path: Path) -> HarnessNetworkConfig | None:
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
    return HarnessNetworkConfig(path=config_path.as_posix(), bypasses_enforcement=True)


def read_codex_network_configs(
    env: Mapping[str, str],
) -> tuple[HarnessNetworkConfig, ...]:
    """Detect widening in the child-selected user and project Codex configs."""

    project_root = Path(env.get("MERIDIAN_PROJECT_DIR", "") or Path.cwd())
    codex_home = resolve_codex_home(env)
    if not codex_home.is_absolute():
        codex_home = project_root / codex_home
    paths = (
        codex_home / "config.toml",
        project_root / ".codex" / "config.toml",
    )
    configs: list[HarnessNetworkConfig] = []
    for path in dict.fromkeys(paths):
        config = _read_network_config(path)
        if config is not None:
            configs.append(config)
    return tuple(configs)


__all__ = ["read_codex_network_configs"]
