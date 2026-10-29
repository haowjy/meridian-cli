"""Build a ``LaunchPaths`` for a test that owns its runtime root."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import LaunchPaths


def launch_paths(runtime_root: Path, spawn_id: SpawnId | str) -> LaunchPaths:
    return LaunchPaths(runtime_root, SpawnId(str(spawn_id)), None)
