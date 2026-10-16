"""Canonical Pi filesystem paths for Meridian harness launches.

Spawned RPC sessions keep per-spawn session files under Meridian state, but agent
config (auth, extension materialization) uses the same tree as interactive ``pi``.
Meridian extension bundles are read from a stable install root; runtime extension
state uses ``_MERIDIAN_PI_STATE_DIR``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.platform import expand_home_path, get_home_path
from meridian.lib.state.user_paths import get_user_home

_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_PI_STATE_DIR_ENV = "_MERIDIAN_PI_STATE_DIR"
_MERIDIAN_EXTENSION_NAMESPACE = "meridian"
_MERIDIAN_PI_EXTENSION_ROOT_ENV = "MERIDIAN_PI_EXTENSION_INSTALL_ROOT"


def spawn_scope_dir(runtime_root: Path, spawn_id: SpawnId) -> Path:
    """Return Pi state shared by every run of a spawn.

    ``observed-spawns.json``, ``observed-spawns.lock``, and
    ``cleared-spawns.json`` are spawn-scoped, matching
    ``resolveSpawnScopeDir`` in ``pi_state_paths.ts``.
    """

    return runtime_root / "pi-bash" / str(spawn_id)


def run_scope_dir(runtime_root: Path, run_id: RunId) -> Path:
    """Return Pi state owned by one exact run.

    ``bash-records.json``, ``delivery-receipts.json``, ``delivery-fault.json``,
    and ``logs/`` are run-scoped, matching ``resolveRunScopeDir`` in
    ``pi_state_paths.ts``. Meridian's ``delivery-observations.json`` lives with
    the receipts it acknowledges.
    """

    return runtime_root / "pi-bash" / str(run_id)


def resolve_pi_agent_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Pi agent dir (auth, settings) — default ``~/.pi/agent``."""

    override = (os.environ if env is None else env).get(_PI_AGENT_DIR_ENV, "").strip()
    if override:
        return expand_home_path(override, env=env)
    return get_home_path(env=env) / ".pi" / "agent"


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

    override = (os.environ if env is None else env).get(_PI_SESSION_DIR_ENV, "").strip()
    if override:
        return expand_home_path(override, env=env)
    return get_user_home(env=env) / "meridian-pi" / "sessions"


def resolve_meridian_pi_state_dir(*, env: Mapping[str, str] | None = None) -> Path:
    """Return Meridian Pi extension runtime state root."""

    if env is not None:
        explicit = env.get(_PI_STATE_DIR_ENV, "").strip()
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
    return os.environ.get(key, "").strip()


def pi_meridian_state_dir_env_override(
    *,
    env: Mapping[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, str]:
    """Env override for extension runtime state (tasks, spawn-watch tree)."""

    if runtime_root is not None:
        return {_PI_STATE_DIR_ENV: str(runtime_root)}
    return {_PI_STATE_DIR_ENV: str(resolve_meridian_pi_state_dir(env=env))}


__all__ = [
    "pi_meridian_state_dir_env_override",
    "resolve_meridian_pi_extension_root",
    "resolve_meridian_pi_state_dir",
    "resolve_pi_agent_dir",
    "resolve_pi_default_user_extension_dir",
    "resolve_pi_extension_target_root",
    "resolve_pi_spawn_session_root",
    "run_scope_dir",
    "spawn_scope_dir",
]
