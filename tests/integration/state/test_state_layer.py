"""State-layer tests for file-authoritative stores."""

import json
import multiprocessing
from pathlib import Path
from typing import Any, TypedDict

from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    list_spawns,
    reserve_spawn_id,
    start_spawn,
    update_spawn,
)


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


class _ExecutionUpdatePayload(TypedDict):
    execution_cwd: str
    worker_pid: int


class _HarnessUpdatePayload(TypedDict):
    harness_session_id: str
    claude_config_dir: str


def _reserve_id_in_subprocess(
    runtime_root_str: str,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    runtime_root = Path(runtime_root_str)
    ready_queue.put("ready")
    start_event.wait()
    reserved = reserve_spawn_id(runtime_root)
    result_queue.put(str(reserved))


def _concurrent_update_in_subprocess(
    runtime_root_str: str,
    spawn_id: str,
    payload: _ExecutionUpdatePayload | _HarnessUpdatePayload,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    runtime_root = Path(runtime_root_str)
    ready_queue.put("ready")
    start_event.wait()
    if "worker_pid" in payload:
        update_spawn(
            runtime_root,
            spawn_id,
            execution_cwd=payload["execution_cwd"],
            worker_pid=payload["worker_pid"],
        )
    else:
        update_spawn(
            runtime_root,
            spawn_id,
            harness_session_id=payload["harness_session_id"],
            claude_config_dir=payload["claude_config_dir"],
        )
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    result_queue.put(row.model_dump())


def _read_after_concurrent_migration(
    runtime_root_str: str,
    spawn_id: str,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    runtime_root = Path(runtime_root_str)
    ready_queue.put("ready")
    start_event.wait()
    row = get_spawn(runtime_root, spawn_id)
    spawns = list_spawns(runtime_root)
    result_queue.put(
        {
            "row_status": None if row is None else row.status,
            "row_prompt": None if row is None else row.prompt,
            "ids": [spawn.id for spawn in spawns],
            "marker": (runtime_root / "spawns" / "v2-format.json").is_file(),
        }
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


def test_cross_process_reserve_spawn_id_returns_unique_contiguous_ids(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    process_count = 6

    ctx = multiprocessing.get_context("spawn")
    ready_queue = ctx.Queue()
    result_queue = ctx.Queue()
    start_event = ctx.Event()
    processes = [
        ctx.Process(
            target=_reserve_id_in_subprocess,
            args=(runtime_root.as_posix(), ready_queue, start_event, result_queue),
        )
        for _ in range(process_count)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready_queue.get(timeout=10) == "ready"
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    reserved = sorted(result_queue.get(timeout=10) for _ in processes)

    assert reserved == [f"p{idx}" for idx in range(1, process_count + 1)]
    assert (runtime_root / "spawn-id-counter").read_text(encoding="utf-8").strip() == str(
        process_count
    )


def test_reserve_spawn_id_keeps_stable_lock_path_after_v2_marker(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)

    assert str(reserve_spawn_id(runtime_root)) == "p1"
    assert str(reserve_spawn_id(runtime_root)) == "p2"

    assert (runtime_root / "spawns.jsonl.flock").is_file()
    assert not (runtime_root / "spawns.legacy-v1.jsonl.flock").exists()


def test_concurrent_update_spawn_preserves_non_overlapping_fields(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
        )
    )

    ctx = multiprocessing.get_context("spawn")
    ready_queue = ctx.Queue()
    result_queue = ctx.Queue()
    start_event = ctx.Event()
    payloads: list[_ExecutionUpdatePayload | _HarnessUpdatePayload] = [
        {"execution_cwd": "/tmp/job", "worker_pid": 101},
        {"harness_session_id": "sess-123", "claude_config_dir": "/tmp/claude"},
    ]
    processes = [
        ctx.Process(
            target=_concurrent_update_in_subprocess,
            args=(
                runtime_root.as_posix(),
                spawn_id,
                payload,
                ready_queue,
                start_event,
                result_queue,
            ),
        )
        for payload in payloads
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready_queue.get(timeout=10) == "ready"
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    snapshots = [result_queue.get(timeout=10) for _ in processes]
    row = get_spawn(runtime_root, spawn_id)

    assert row is not None
    assert row.execution_cwd == "/tmp/job"
    assert row.worker_pid == 101
    assert row.harness_session_id == "sess-123"
    assert row.claude_config_dir == "/tmp/claude"

    revisions = [
        json.loads((runtime_root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8"))[
            "revision"
        ]
    ]
    assert revisions == [3]
    assert any(snapshot["execution_cwd"] == "/tmp/job" for snapshot in snapshots)
    assert any(snapshot["harness_session_id"] == "sess-123" for snapshot in snapshots)


def test_concurrent_first_reader_migration_converges_for_both_processes(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / "spawns.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "start",
                    "id": "p1",
                    "chat_id": "c1",
                    "model": "gpt-5.4",
                    "agent": "coder",
                    "harness": "codex",
                    "kind": "child",
                    "prompt": "legacy",
                    "status": "running",
                    "started_at": "2026-01-01T00:00:00Z",
                },
                sort_keys=True,
            )
            + "\n"
        )

    ctx = multiprocessing.get_context("spawn")
    ready_queue = ctx.Queue()
    result_queue = ctx.Queue()
    start_event = ctx.Event()
    processes = [
        ctx.Process(
            target=_read_after_concurrent_migration,
            args=(runtime_root.as_posix(), "p1", ready_queue, start_event, result_queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        assert ready_queue.get(timeout=10) == "ready"
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [result_queue.get(timeout=10) for _ in processes]

    assert all(result["row_status"] == "running" for result in results)
    assert all(result["row_prompt"] == "legacy" for result in results)
    assert all(result["ids"] == ["p1"] for result in results)
    assert all(result["marker"] is True for result in results)
    assert (runtime_root / "spawns" / "p1" / "state.json").is_file()
    assert (runtime_root / "spawns.legacy-v1.jsonl").is_file()


def test_owner_finalize_drops_stale_terminal_write_after_external_winner(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    service = SpawnLifecycleService(runtime_root)
    spawn_id = service.start(
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
    )

    external = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
        duration_secs=1.0,
    )
    stale_owner = service.finalize(
        spawn_id,
        status="failed",
        exit_code=1,
        origin="launcher",
        error="stale owner",
    )
    row = get_spawn(runtime_root, spawn_id)

    assert external.wrote is True
    assert stale_owner.wrote is False
    assert stale_owner.transitioned is False
    assert stale_owner.snapshot is not None
    assert stale_owner.snapshot.status == "succeeded"
    assert row is not None
    assert row.status == "succeeded"
    assert row.error is None
    assert row.duration_secs == 1.0
