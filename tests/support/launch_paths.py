"""Build a ``LaunchPaths`` for a test that owns its runtime root."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_store import get_spawn


def launch_paths(runtime_root: Path, spawn_id: SpawnId | str) -> LaunchPaths:
    """The launch of a published row, built the way production builds it: from the row.

    A test that never publishes a row gets a preview launch under the same ID.
    """

    row = get_spawn(runtime_root, spawn_id)
    if row is None:
        return LaunchPaths.preview(runtime_root, str(spawn_id))
    return LaunchPaths.for_row(runtime_root, row)
