"""Heartbeat writes respect spawn-directory lifetime."""

import shutil
from pathlib import Path

from meridian.lib.launch.streaming.heartbeat import FileHeartbeat


def test_heartbeat_does_not_recreate_deleted_parent(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p1"
    heartbeat_path = spawn_dir / "heartbeat"
    spawn_dir.mkdir(parents=True)
    heartbeat = FileHeartbeat(heartbeat_path)
    heartbeat.touch()
    assert heartbeat_path.is_file()

    shutil.rmtree(spawn_dir)
    heartbeat.touch()

    assert not spawn_dir.exists()
