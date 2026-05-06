"""State-layer tests for file-authoritative stores."""

import multiprocessing
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn_store import finalize_spawn, list_spawns, start_spawn


def _write_start_and_finalize(project_root: str, idx: int) -> None:
    root = Path(project_root)
    runtime_root = resolve_runtime_paths(root).root_dir
    spawn_id = SpawnId(f"rlock{idx}")
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=f"c{idx}",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt=f"job-{idx}",
        started_at="2026-02-25T00:00:00Z",
    )
    finalize_spawn(
        runtime_root,
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=0.1,
        finished_at="2026-02-25T00:00:01Z",
    )


def test_locking_contention_writes_clean_v2_state(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    process_count = 8

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_write_start_and_finalize, args=(tmp_path.as_posix(), idx))
        for idx in range(process_count)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=20)
        assert proc.exitcode == 0

    rows = list_spawns(runtime_root)
    assert len(rows) == process_count
    assert all(row.status == "succeeded" for row in rows)

    assert len(list((runtime_root / "spawns").glob("*/state.json"))) == process_count
