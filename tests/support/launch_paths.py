"""Build a ``LaunchPaths`` for a test that owns its runtime root."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn_store import get_spawn


def launch_paths(runtime_root: Path, spawn_id: SpawnId | str) -> LaunchPaths:
    """The launch of a published row, built the way production builds it: from the row.

    Before the row is published this is the first launch ``start_spawn`` will publish
    (``reserved``, as ``reserve_launch`` hands it out), so a test that writes first and
    publishes later sees one layout. Never a launch outside ``launches/<n>/``.
    """

    row = get_spawn(runtime_root, spawn_id)
    if row is None:
        return LaunchPaths.reserved(runtime_root, SpawnId(str(spawn_id)), 1)
    return LaunchPaths.for_row(runtime_root, row)
