"""Persistence for the best-effort spawn failure sentinel."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

logger = logging.getLogger(__name__)


def write_failure_sentinel(
    runtime_root: Path, spawn_id: str, payload: Mapping[str, object]
) -> None:
    """Best-effort atomic write of a failure sentinel."""

    try:
        path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "failure.json"
        mutate_published_spawn_artifact(
            runtime_root,
            spawn_id,
            lambda: atomic_write_text(path, json.dumps(payload, indent=2)),
            can_mutate=lambda record: record.status == "failed",
        )
    except Exception:
        logger.exception("Failed to write failure sentinel for %s", spawn_id)


def delete_failure_sentinel(runtime_root: Path, spawn_id: str) -> None:
    """Best-effort removal of a stale failure sentinel."""

    try:
        path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "failure.json"
        path.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to remove failure sentinel for %s", spawn_id)


__all__ = ["delete_failure_sentinel", "write_failure_sentinel"]
