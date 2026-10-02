"""Pi runtime metadata respects the published spawn's lifetime."""

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.pi import _write_pi_runtime_metadata_sidecar
from meridian.lib.state.spawn_store import start_spawn


def test_pi_metadata_never_recreates_missing_spawn(tmp_path: Path) -> None:
    spawn_id = SpawnId("metadata-owner")
    payload = {"runtime_path": "/runtime/pi"}
    _write_pi_runtime_metadata_sidecar(runtime_root=tmp_path, spawn_id=spawn_id, payload=payload)
    assert not (tmp_path / "spawns" / str(spawn_id)).exists()
    start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test",
        agent="tester",
        harness="pi",
        prompt="test",
        status="running",
    )
    _write_pi_runtime_metadata_sidecar(runtime_root=tmp_path, spawn_id=spawn_id, payload=payload)
    assert (tmp_path / "spawns" / str(spawn_id) / "pi_runtime_meta.json").is_file()
