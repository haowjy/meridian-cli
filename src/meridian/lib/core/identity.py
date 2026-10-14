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


def current_run_id(env: Mapping[str, str] | None = None) -> RunId | None:
    """Exact launching run; never consult the spawn's current/latest run.

    Older launchers only set SPAWN_ID. They cannot create runs >=2, so that
    fallback remains exact even after a tool upgrade under a running harness.
    """
    source = os.environ if env is None else env
    value = (source.get("MERIDIAN_RUN_ID") or "").strip()
    value = value or (source.get("MERIDIAN_SPAWN_ID") or "").strip()
    return parse_ref(value, row_key=True) if value else None
