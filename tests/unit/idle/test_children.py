from pathlib import Path

from meridian.lib.idle.children import SpawnRow, active_child_count, spawn_store_reader
from meridian.lib.state import spawn_store
from meridian.lib.state.launch_lock import LaunchSlot


def test_active_child_count_derives_liveness_and_tolerates_cycles(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    reader = spawn_store_reader({"_MERIDIAN_RUNTIME_DIR": str(runtime_root)})

    with LaunchSlot() as launch_lock:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p2",
            parent_id="p1",
            chat_id="c1",
            model="test",
            agent="coder",
            harness="codex",
            prompt="hello",
            launch_lock=launch_lock,
        )
        assert active_child_count("p1", reader()) == 1

    assert active_child_count("p1", reader()) == 0
    cyclic_terminal_rows = (
        SpawnRow("p2", "p1", "succeeded"),
        SpawnRow("p1", "p2", "succeeded"),
    )
    assert active_child_count("p1", cyclic_terminal_rows) == 0
    assert active_child_count(None, cyclic_terminal_rows) == 0
