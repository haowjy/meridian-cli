"""Canonical Pi filesystem paths for Meridian harness launches.

Spawned RPC sessions keep per-spawn session files under Meridian state, but agent
config (auth, extension materialization) uses the same tree as interactive ``pi``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.state.user_paths import get_user_home

_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_MERIDIAN_EXTENSION_NAMESPACE = "meridian"


def resolve_pi_agent_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Pi agent dir (auth, settings) — default ``~/.pi/agent``."""

    if env is not None:
        override = env.get(_PI_AGENT_DIR_ENV, "").strip()
        if override:
            return Path(override).expanduser()
    return Path.home() / ".pi" / "agent"


def resolve_pi_spawn_session_root(*, env: Mapping[str, str] | None = None) -> Path:
    """Return unscoped spawn session root (per-spawn subdirs applied later)."""

    if env is not None:
        override = env.get(_PI_SESSION_DIR_ENV, "").strip()
        if override:
            return Path(override).expanduser()
    return get_user_home() / "meridian-pi" / "sessions"


def resolve_pi_extension_target_root(
    launch_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Directory for one launch's materialized Meridian ``-e`` extensions."""

    return (
        resolve_pi_agent_dir(env=env)
        / "extensions"
        / _MERIDIAN_EXTENSION_NAMESPACE
        / launch_id
    )


def pi_agent_dir_env_override() -> dict[str, str]:
    """Env overrides so subprocess Pi uses the standard agent tree."""

    return {_PI_AGENT_DIR_ENV: str(resolve_pi_agent_dir())}


def pi_spawn_session_root_env_override() -> dict[str, str]:
    """Env overrides for Meridian-managed spawn session storage."""

    return {_PI_SESSION_DIR_ENV: str(resolve_pi_spawn_session_root())}


__all__ = [
    "pi_agent_dir_env_override",
    "pi_spawn_session_root_env_override",
    "resolve_pi_agent_dir",
    "resolve_pi_extension_target_root",
    "resolve_pi_spawn_session_root",
]
