"""The only readers of public process identity environment variables."""

import os
from collections.abc import Mapping

from meridian.lib.core.refs import parse_ref, spawn_of
from meridian.lib.core.types import RunId, SpawnId


def current_spawn_id(env: Mapping[str, str] | None = None) -> SpawnId | None:
    """Stable conversation identity, including for a process launched by an older binary."""
    source = os.environ if env is None else env
    value = (source.get("MERIDIAN_SPAWN_ID") or "").strip()
    return spawn_of(value) if value else None


def current_parent_spawn_id(env: Mapping[str, str] | None = None) -> SpawnId | None:
    """Stable parent identity inherited by a child process, when present."""

    source = os.environ if env is None else env
    value = (source.get("_MERIDIAN_PARENT_SPAWN_ID") or "").strip()
    return spawn_of(value) if value else None


def current_run_id(env: Mapping[str, str] | None = None) -> RunId | None:
    """Exact launching run; never consult the spawn's current/latest run.

    Older launchers only set SPAWN_ID. They cannot create runs >=2, so that
    fallback remains exact even after a tool upgrade under a running harness.
    """
    source = os.environ if env is None else env
    run_value = (source.get("MERIDIAN_RUN_ID") or "").strip()
    spawn_value = (source.get("MERIDIAN_SPAWN_ID") or "").strip()
    # Compatibility belongs only at this environment read boundary. Launchers
    # predating run IDs emitted just MERIDIAN_SPAWN_ID, and could only launch
    # run 1. An inherited run ID from a newer parent does not describe a child
    # launched by that older binary, so prefer the old launcher's spawn value.
    value = (
        run_value
        if run_value and (not spawn_value or spawn_of(run_value) == spawn_value)
        else spawn_value
    )
    return parse_ref(value, row_key=True) if value else None


def identity_env(spawn_id: SpawnId, run_id: RunId) -> dict[str, str]:
    """Encode one stable/exact identity pair for a child launch environment."""
    if spawn_of(run_id) != spawn_id:
        raise ValueError(f"Run '{run_id}' does not belong to spawn '{spawn_id}'")
    return {
        "MERIDIAN_SPAWN_ID": str(spawn_id),
        "MERIDIAN_RUN_ID": str(run_id),
    }


def identity_ids(run_id: RunId) -> dict[str, str]:
    """Project one exact run into the stable/exact wire identity pair."""
    return {"spawn_id": str(spawn_of(run_id)), "run_id": str(run_id)}
