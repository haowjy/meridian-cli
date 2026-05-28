"""Canonical Pi filesystem paths for Meridian harness launches.

Spawned RPC sessions keep per-spawn session files under Meridian state, but agent
config (auth, extension materialization) uses the same tree as interactive ``pi``.
Meridian extension bundles are read from a stable install root; runtime extension
state uses ``MERIDIAN_PI_STATE_DIR``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.state.user_paths import get_user_home

_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_MERIDIAN_PI_STATE_DIR_ENV = "MERIDIAN_PI_STATE_DIR"
_MERIDIAN_EXTENSION_NAMESPACE = "meridian"
_MERIDIAN_PI_EXTENSION_ROOT_ENV = "MERIDIAN_PI_EXTENSION_INSTALL_ROOT"


def resolve_pi_agent_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Pi agent dir (auth, settings) — default ``~/.pi/agent``."""

    if env is not None:
        override = env.get(_PI_AGENT_DIR_ENV, "").strip()
        if override:
            return Path(override).expanduser()
    return Path.home() / ".pi" / "agent"


def resolve_pi_default_user_extension_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Pi's default user extension discovery root."""

    return resolve_pi_agent_dir(env=env) / "extensions"


def resolve_meridian_pi_extension_root() -> Path:
    """Return Meridian-shipped Pi extension bundles (stable ``-e`` targets)."""

    override = _env_strip(_MERIDIAN_PI_EXTENSION_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return get_user_home() / "pi" / "extensions"


def resolve_pi_spawn_session_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Return unscoped spawn session root (per-spawn subdirs applied later)."""

    if env is not None:
        override = env.get(_PI_SESSION_DIR_ENV, "").strip()
        if override:
            return Path(override).expanduser()
    return get_user_home() / "meridian-pi" / "sessions"


def resolve_meridian_pi_state_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Meridian Pi extension runtime state root."""

    if env is not None:
        explicit = env.get(_MERIDIAN_PI_STATE_DIR_ENV, "").strip()
        if explicit:
            return Path(explicit).expanduser()
    return get_user_home() / "meridian-pi" / "state"


def resolve_pi_extension_target_root(
    launch_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Legacy per-launch materialization dir (dev/tests only — not hot path)."""

    return (
        resolve_pi_agent_dir(env=env)
        / "extensions"
        / _MERIDIAN_EXTENSION_NAMESPACE
        / launch_id
    )


def _env_strip(key: str) -> str:
    import os

    return os.environ.get(key, "").strip()


def pi_agent_dir_env_override() -> dict[str, str]:
    """Env overrides so subprocess Pi uses the standard agent tree."""

    return {_PI_AGENT_DIR_ENV: str(resolve_pi_agent_dir())}


def pi_spawn_session_root_env_override() -> dict[str, str]:
    """Env overrides for Meridian-managed spawn session storage."""

    return {_PI_SESSION_DIR_ENV: str(resolve_pi_spawn_session_root())}


def pi_meridian_state_dir_env_override(
    *,
    env: Mapping[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, str]:
    """Env override for extension runtime state (tasks, spawn-watch tree)."""

    if runtime_root is not None:
        return {_MERIDIAN_PI_STATE_DIR_ENV: str(runtime_root)}
    return {_MERIDIAN_PI_STATE_DIR_ENV: str(resolve_meridian_pi_state_dir(env=env))}


__all__ = [
    "pi_agent_dir_env_override",
    "pi_meridian_state_dir_env_override",
    "pi_spawn_session_root_env_override",
    "resolve_meridian_pi_extension_root",
    "resolve_meridian_pi_state_dir",
    "resolve_pi_agent_dir",
    "resolve_pi_default_user_extension_dir",
    "resolve_pi_extension_target_root",
    "resolve_pi_spawn_session_root",
]
