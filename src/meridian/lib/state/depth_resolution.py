"""Spawn-record-backed Meridian depth resolution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meridian.lib.core.depth import (
    MERIDIAN_DEPTH_ENV,
    MERIDIAN_SPAWN_ID_ENV,
    parse_meridian_depth,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.state import spawn_store


def depth_from_spawn_ancestry(
    spawn_id: str | SpawnId,
    runtime_root: Path,
) -> int | None:
    """Return spawn depth from persisted records, or ``None`` when unknown."""

    normalized_spawn_id = str(spawn_id).strip()
    if not normalized_spawn_id:
        return None

    record = spawn_store.get_spawn(runtime_root, normalized_spawn_id)
    if record is None:
        return None
    if record.meridian_depth is not None:
        return record.meridian_depth

    depth = 0
    current_id = normalized_spawn_id
    visited: set[str] = set()
    while current_id:
        if current_id in visited:
            return None
        visited.add(current_id)
        walk_record = spawn_store.get_spawn(runtime_root, current_id)
        if walk_record is None:
            if current_id == normalized_spawn_id:
                return None
            return max(depth, record.meridian_depth or 0)
        parent_id = (walk_record.parent_id or "").strip() or None
        if parent_id is None:
            return depth
        depth += 1
        current_id = parent_id
    return depth


def resolve_effective_meridian_depth(
    env: Mapping[str, str],
    *,
    runtime_root: Path | None = None,
) -> int:
    """Return depth using coordinator records when the caller spawn is known."""

    env_depth = parse_meridian_depth(env.get(MERIDIAN_DEPTH_ENV))
    spawn_id = (env.get(MERIDIAN_SPAWN_ID_ENV) or "").strip()
    if not spawn_id or runtime_root is None:
        return env_depth
    record_depth = depth_from_spawn_ancestry(spawn_id, runtime_root)
    if record_depth is None:
        return env_depth
    return max(env_depth, record_depth)


__all__ = [
    "depth_from_spawn_ancestry",
    "resolve_effective_meridian_depth",
]
