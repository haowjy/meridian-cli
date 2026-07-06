"""Canonical helpers for Meridian delegation depth."""

import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from meridian.lib.core.types import SpawnId

MERIDIAN_DEPTH_ENV = "MERIDIAN_DEPTH"
MERIDIAN_SPAWN_ID_ENV = "MERIDIAN_SPAWN_ID"


def parse_meridian_depth(raw: str | None) -> int:
    """Parse a ``MERIDIAN_DEPTH`` value as a non-negative integer."""
    value = (raw or "0").strip()
    with suppress(ValueError, TypeError):
        return max(0, int(value))
    return 0


def current_meridian_depth(env: Mapping[str, str] | None = None) -> int:
    """Return the current process's normalized Meridian depth."""
    source = os.environ if env is None else env
    return parse_meridian_depth(source.get(MERIDIAN_DEPTH_ENV))


def depth_from_spawn_ancestry(
    spawn_id: str | SpawnId,
    runtime_root: Path,
) -> int | None:
    """Return spawn depth from persisted records, or ``None`` when unknown."""

    from meridian.lib.state import spawn_store

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


def has_valid_meridian_depth(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the current depth env value is absent/empty or parseable."""
    source = os.environ if env is None else env
    raw = (source.get(MERIDIAN_DEPTH_ENV) or "").strip()
    if not raw:
        return True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False
    return value >= 0


def child_meridian_depth(parent_depth: int, *, increment_depth: bool = True) -> int:
    """Return the depth to project into a child process."""
    normalized = max(0, parent_depth)
    return normalized + 1 if increment_depth else normalized


def is_nested_meridian_depth(depth: int) -> bool:
    """Return whether a depth represents execution below the primary root."""
    return depth > 0


def is_nested_meridian_process(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the current environment is inside delegated Meridian execution."""
    return is_nested_meridian_depth(current_meridian_depth(env))


def is_managed_meridian_session(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the process runs inside a Meridian-managed agent session.

    A managed session is any harness Meridian launched: the primary root agent
    (depth 0) as well as delegated subagents (depth > 0). This is the right
    signal for "an agent, not a human, is invoking the CLI" — distinct from
    ``is_nested_meridian_process`` (depth > 0), which excludes the primary
    because primary launch keeps depth 0. Keyed on the injected
    ``MERIDIAN_SPAWN_ID`` rather than ``MERIDIAN_MANAGED``, which the entrypoint
    ``setdefault``s on every invocation (including a human's raw shell).
    """
    source = os.environ if env is None else env
    return bool((source.get(MERIDIAN_SPAWN_ID_ENV) or "").strip())


def is_root_side_effect_process(env: Mapping[str, str] | None = None) -> bool:
    """Return whether root-only repair/reaper side effects may run.

    Malformed non-empty depth is fail-closed: normal read paths can normalize
    bad values to depth 0, but root-only cleanup must not run unless the depth
    value clearly represents the primary root.
    """
    source = os.environ if env is None else env
    if not has_valid_meridian_depth(source):
        return False
    return not is_nested_meridian_process(source)


def max_depth_reached(current_depth: int, max_depth: int) -> bool:
    """Return whether creating another depth-child would exceed the configured ceiling."""
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0.")
    return current_depth >= max_depth


__all__ = [
    "MERIDIAN_DEPTH_ENV",
    "MERIDIAN_SPAWN_ID_ENV",
    "child_meridian_depth",
    "current_meridian_depth",
    "depth_from_spawn_ancestry",
    "has_valid_meridian_depth",
    "is_managed_meridian_session",
    "is_nested_meridian_depth",
    "is_nested_meridian_process",
    "is_root_side_effect_process",
    "max_depth_reached",
    "parse_meridian_depth",
    "resolve_effective_meridian_depth",
]
