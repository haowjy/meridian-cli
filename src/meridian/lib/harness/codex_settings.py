"""Read Codex user settings that affect projected network policy."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.harness.codex_rollout import resolve_codex_home
from meridian.lib.safety.policy import UserNetworkConfig, host_set

logger = structlog.get_logger(__name__)


def _unreadable(path: Path, reason: str) -> UserNetworkConfig:
    logger.warning("Failed to parse Codex config", path=str(path), reason=reason)
    return UserNetworkConfig(path=path.as_posix(), unreadable_reason=reason)


def read_codex_user_network_config(
    env: Mapping[str, str],
) -> UserNetworkConfig | None:
    """Return legacy proxy-domain facts from the config selected by the child env."""

    config_path = resolve_codex_home(env) / "config.toml"
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
        return UserNetworkConfig(path=config_path.as_posix())
    if not isinstance(raw_features, dict):
        return _unreadable(config_path, "features is not a table")
    raw_proxy = cast("dict[str, object]", raw_features).get("network_proxy")
    if raw_proxy is None:
        return UserNetworkConfig(path=config_path.as_posix())
    if not isinstance(raw_proxy, dict):
        return _unreadable(config_path, "features.network_proxy is not a table")
    raw_domains = cast("dict[str, object]", raw_proxy).get("domains")
    if raw_domains is None:
        return UserNetworkConfig(path=config_path.as_posix())
    if not isinstance(raw_domains, dict):
        return _unreadable(
            config_path,
            "features.network_proxy.domains is not a table",
        )

    allow: list[str] = []
    deny: list[str] = []
    for raw_host, raw_action in cast("dict[object, object]", raw_domains).items():
        if not isinstance(raw_host, str) or not raw_host.strip():
            return _unreadable(config_path, "network proxy domain host is empty")
        if raw_action == "allow":
            allow.append(raw_host)
        elif raw_action == "deny":
            deny.append(raw_host)
        else:
            return _unreadable(
                config_path,
                f"network proxy domain {raw_host!r} is not allow or deny",
            )

    return UserNetworkConfig(
        path=config_path.as_posix(),
        allow=host_set(allow),
        deny=host_set(deny),
    )


__all__ = ["read_codex_user_network_config"]
