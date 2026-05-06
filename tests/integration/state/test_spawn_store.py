import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    list_spawns,
    mark_finalizing,
    mark_spawn_running,
    next_spawn_id,
    record_spawn_exited,
    reserve_spawn_id,
    start_spawn,
    update_spawn,
)


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(runtime_root: Path) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
        )
    )


def _state_revision(runtime_root: Path, spawn_id: str) -> int:
    payload = json.loads(
        (runtime_root / "spawns" / spawn_id / "state.json").read_text(encoding="utf-8")
    )
    return int(payload["revision"])


def _append_legacy_event(runtime_root: Path, payload: dict[str, object]) -> None:
    with (runtime_root / "spawns.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def test_lazy_migration_converts_legacy_jsonl_to_v2_state(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    _append_legacy_event(
        runtime_root,
        {
            "v": 1,
            "event": "start",
            "id": "p1",
            "chat_id": "c1",
            "model": "gpt-5.4",
            "agent": "coder",
            "harness": "codex",
            "kind": "child",
            "prompt": "legacy prompt",
            "status": "running",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    _append_legacy_event(
        runtime_root,
        {
            "v": 1,
            "event": "finalize",
            "id": "p1",
            "status": "succeeded",
            "exit_code": 0,
            "finished_at": "2026-01-01T00:01:00Z",
            "origin": "runner",
        },
    )

    row = get_spawn(runtime_root, "p1")

    assert row is not None
    assert row.status == "succeeded"
    assert row.prompt == "legacy prompt"
    assert (runtime_root / "spawns" / "v2-format.json").is_file()
    assert (runtime_root / "spawns" / "p1" / "state.json").is_file()
    assert (runtime_root / "spawns.legacy-v1.jsonl").is_file()
    assert not (runtime_root / "spawns.jsonl").exists()


def test_lazy_migration_converts_active_legacy_rows_without_deferring(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    _append_legacy_event(
        runtime_root,
        {
            "v": 1,
            "event": "start",
            "id": "p1",
            "chat_id": "c1",
            "model": "gpt-5.4",
            "agent": "coder",
            "harness": "codex",
            "kind": "child",
            "prompt": "active legacy",
            "status": "running",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    row = get_spawn(runtime_root, "p1")

    assert row is not None
    assert row.status == "running"
    assert row.prompt == "active legacy"
    assert (runtime_root / "spawns" / "v2-format.json").is_file()
    assert (runtime_root / "spawns" / "p1" / "state.json").is_file()
    assert (runtime_root / "spawns.legacy-v1.jsonl").is_file()
    assert not (runtime_root / "spawns.jsonl").exists()


def test_migration_ignores_malformed_legacy_lines(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawns_jsonl = runtime_root / "spawns.jsonl"
    spawns_jsonl.write_text(
        "{not-json}\n"
        + json.dumps(
            {
                "v": 1,
                "event": "start",
                "id": "p1",
                "chat_id": "c1",
                "model": "gpt-5.4",
                "agent": "coder",
                "harness": "codex",
                "kind": "child",
                "prompt": "valid",
                "status": "running",
                "started_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n"
        + json.dumps({"v": 1, "event": "finalize", "id": "p1"}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    row = get_spawn(runtime_root, "p1")

    assert row is not None
    assert row.status == "running"
    assert row.prompt == "valid"
    assert list_spawns(runtime_root)[0].id == "p1"


def test_migration_is_idempotent_after_marker_and_ignores_recreated_legacy_jsonl(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    _append_legacy_event(
        runtime_root,
        {
            "v": 1,
            "event": "start",
            "id": "p1",
            "chat_id": "c1",
            "model": "gpt-5.4",
            "agent": "coder",
            "harness": "codex",
            "kind": "child",
            "prompt": "legacy prompt",
            "status": "running",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    first = get_spawn(runtime_root, "p1")
    assert first is not None

    _append_legacy_event(
        runtime_root,
        {
            "v": 1,
            "event": "start",
            "id": "p999",
            "chat_id": "cx",
            "model": "gpt-5.4",
            "agent": "ignored",
            "harness": "codex",
            "kind": "child",
            "prompt": "should be ignored",
            "status": "running",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )

    spawns = list_spawns(runtime_root)

    assert [spawn.id for spawn in spawns] == ["p1"]
    assert get_spawn(runtime_root, "p999") is None
    assert (runtime_root / "spawns" / "p999").exists() is False


def test_migration_retry_after_partial_failure_converges(tmp_path: Path, monkeypatch: Any) -> None:
    runtime_root = _state_root(tmp_path)
    for spawn_id, prompt in (("p1", "first"), ("p2", "second")):
        _append_legacy_event(
            runtime_root,
            {
                "v": 1,
                "event": "start",
                "id": spawn_id,
                "chat_id": f"c-{spawn_id}",
                "model": "gpt-5.4",
                "agent": "coder",
                "harness": "codex",
                "kind": "child",
                "prompt": prompt,
                "status": "running",
                "started_at": "2026-01-01T00:00:00Z",
            },
        )

    from meridian.lib.state.spawn import migration

    real_write_state = migration.write_state
    calls = {"count": 0}

    def flaky_write_state(*args: Any, **kwargs: Any) -> int:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated migration crash before marker")
        return real_write_state(*args, **kwargs)

    monkeypatch.setattr(migration, "write_state", flaky_write_state)

    try:
        get_spawn(runtime_root, "p1")
    except RuntimeError as exc:
        assert "simulated migration crash" in str(exc)
    else:
        raise AssertionError("expected migration failure")

    assert (runtime_root / "spawns" / "v2-format.json").exists() is False
    assert (runtime_root / "spawns" / "p1" / "state.json").is_file()

    monkeypatch.setattr(migration, "write_state", real_write_state)
    recovered = get_spawn(runtime_root, "p2")

    assert recovered is not None
    assert recovered.prompt == "second"
    assert sorted(spawn.id for spawn in list_spawns(runtime_root)) == ["p1", "p2"]
    assert (runtime_root / "spawns" / "v2-format.json").is_file()
    assert (runtime_root / "spawns.legacy-v1.jsonl").is_file()
    assert not (runtime_root / "spawns.jsonl").exists()


def _finalize_in_subprocess(
    runtime_root_str: str,
    spawn_id: str,
    status: SpawnStatus,
    exit_code: int,
    duration_secs: float,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    ready_queue.put("ready")
    start_event.wait()
    outcome = finalize_spawn(
        Path(runtime_root_str),
        spawn_id,
        status=status,
        exit_code=exit_code,
        origin="runner",
        duration_secs=duration_secs,
    )
    result_queue.put((outcome.wrote, outcome.transitioned))


def test_start_and_update_project_fields_round_trip(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            launch_mode="app",
            runner_pid=1111,
        )
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.launch_mode == "app"
    assert row.runner_pid == 1111

    update_spawn(runtime_root, spawn_id, launch_mode="foreground", runner_pid=2222)
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.launch_mode == "foreground"
    assert row.runner_pid == 2222


def test_list_spawns_filters_v2_rows_and_keeps_listings_promptless(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    p1 = _start_test_spawn(runtime_root)
    p2 = str(
        start_spawn(
            runtime_root,
            chat_id="c2",
            model="gpt-5.4",
            agent="reviewer",
            harness="codex",
            prompt="world",
            desc="desc-2",
        )
    )
    finalize_spawn(runtime_root, p1, status="succeeded", exit_code=0, origin="runner")

    filtered = list_spawns(runtime_root, filters={"status": "running", "agent": "reviewer"})

    assert [spawn.id for spawn in filtered] == [p2]
    assert filtered[0].prompt is None
    assert filtered[0].desc == "desc-2"


def test_get_spawn_returns_none_and_list_spawns_skips_directory_without_state(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    _start_test_spawn(runtime_root)
    ghost_dir = runtime_root / "spawns" / "pghost"
    ghost_dir.mkdir(parents=True, exist_ok=True)
    (ghost_dir / "starting-prompt.md").write_text("dangling", encoding="utf-8")

    assert get_spawn(runtime_root, "pghost") is None
    assert [spawn.id for spawn in list_spawns(runtime_root)] == ["p1"]


def test_spawn_queries_read_v2_state_and_prompt(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    p1 = _start_test_spawn(runtime_root)
    p2 = str(
        start_spawn(
            runtime_root,
            chat_id="c2",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="world",
        )
    )
    finalize_spawn(runtime_root, p1, status="succeeded", exit_code=0, origin="runner")

    spawns = list_spawns(runtime_root)
    assert [spawn.id for spawn in spawns] == [p1, p2]
    assert spawns[0].status == "succeeded"
    assert spawns[1].status == "running"
    assert all(spawn.prompt is None for spawn in spawns)

    row = get_spawn(runtime_root, p2)
    assert row is not None
    assert row.id == p2
    assert row.chat_id == "c2"
    assert row.status == "running"
    assert row.prompt == "world"


def test_next_and_reserved_spawn_ids_ignore_opaque_dirs_and_honor_highest_seed(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawns_dir = runtime_root / "spawns"
    spawns_dir.mkdir(parents=True, exist_ok=True)
    for spawn_id in ("abc", "p7x", "p5", "p12"):
        spawn_dir = spawns_dir / spawn_id
        spawn_dir.mkdir(parents=True, exist_ok=True)
        if spawn_id.startswith("p") and spawn_id[1:].isdigit():
            state_payload = f'{{"v":2,"revision":1,"id":"{spawn_id}"}}\n'
            (spawn_dir / "state.json").write_text(state_payload, encoding="utf-8")
    (runtime_root / "spawn-id-counter").write_text("20\n", encoding="utf-8")

    assert next_spawn_id(runtime_root) == "p21"
    assert reserve_spawn_id(runtime_root) == "p21"
    assert next_spawn_id(runtime_root) == "p22"
    assert (runtime_root / "spawn-id-counter").read_text(encoding="utf-8").strip() == "21"


def test_mark_finalizing_state_machine_enforces_running_only(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    running_spawn_id = _start_test_spawn(runtime_root)

    assert mark_finalizing(runtime_root, running_spawn_id) is True
    row = get_spawn(runtime_root, running_spawn_id)
    assert row is not None
    assert row.status == "finalizing"
    assert mark_finalizing(runtime_root, "p-missing") is False

    non_running_statuses: tuple[SpawnStatus, ...] = (
        "queued",
        "finalizing",
        "succeeded",
        "failed",
        "cancelled",
    )
    for start_status in non_running_statuses:
        spawn_id = str(
            start_spawn(
                runtime_root,
                chat_id=f"c-{start_status}",
                model="gpt-5.4",
                agent="coder",
                harness="codex",
                prompt="hello",
                status=start_status,
            )
        )
        assert mark_finalizing(runtime_root, spawn_id) is False
        row = get_spawn(runtime_root, spawn_id)
        assert row is not None
        assert row.status == start_status


def test_mark_spawn_running_missing_id_does_not_create_phantom_row(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)

    assert mark_spawn_running(runtime_root, "p-missing") is False
    assert get_spawn(runtime_root, "p-missing") is None
    assert list_spawns(runtime_root) == []


def test_mark_finalizing_concurrent_race_only_one_writer_wins(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def attempt(_unused: int) -> bool:
        return mark_finalizing(runtime_root, spawn_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))

    assert sorted(results) == [False, True]
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "finalizing"


def test_projection_authority_reconciler_then_runner_replaces_terminal_tuple(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    reconciler_outcome = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="reconciler",
        error="orphan_run",
    )
    runner_outcome = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
        duration_secs=12.5,
    )
    assert reconciler_outcome.transitioned is True
    assert runner_outcome.transitioned is False
    assert runner_outcome.wrote is True
    assert runner_outcome.snapshot is not None
    assert runner_outcome.snapshot.status == "succeeded"

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert row.terminal_origin == "runner"
    assert row.duration_secs == 12.5


def test_finalize_rejects_losing_authoritative_after_terminal(tmp_path: Path) -> None:
    """CR2.1: A second authoritative finalizer is rejected."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    first = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
    )
    second = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="launcher",
        duration_secs=99.0,
        total_cost_usd=5.0,
        error="loser",
    )

    assert first.wrote is True
    assert first.transitioned is True
    assert second.wrote is False
    assert second.transitioned is False
    assert _state_revision(runtime_root, spawn_id) == 2

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.duration_secs is None
    assert row.total_cost_usd is None
    assert row.error is None
    assert row.terminal_origin == "runner"


def test_losing_authoritative_finalize_does_not_overwrite_winner_metrics(
    tmp_path: Path,
) -> None:
    """CR2.2: rejected terminal events do not affect the projected row."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    first = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
        duration_secs=30.0,
        total_cost_usd=2.0,
        input_tokens=100,
        output_tokens=50,
        finished_at="2026-01-01T00:01:00Z",
    )
    second = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="launcher",
        duration_secs=99.0,
        total_cost_usd=99.0,
        input_tokens=999,
        output_tokens=999,
        error="wrong",
        finished_at="2026-01-01T00:02:00Z",
    )

    assert first.wrote is True
    assert second.wrote is False
    assert _state_revision(runtime_root, spawn_id) == 2

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.terminal_origin == "runner"
    assert row.duration_secs == 30.0
    assert row.total_cost_usd == 2.0
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.finished_at == "2026-01-01T00:01:00Z"
    assert row.error is None


def test_finalize_allows_authoritative_to_replace_reconciler_terminal(
    tmp_path: Path,
) -> None:
    """CR2.3: Authoritative finalize replaces reconciler terminal."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    reconciler = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="reconciler",
        error="orphan_run",
    )
    runner = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
        duration_secs=30.0,
        total_cost_usd=2.0,
    )

    assert reconciler.wrote is True
    assert reconciler.transitioned is True
    assert runner.wrote is True
    assert runner.transitioned is False
    assert _state_revision(runtime_root, spawn_id) == 3

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert row.terminal_origin == "runner"
    assert row.duration_secs == 30.0
    assert row.total_cost_usd == 2.0


def test_concurrent_authoritative_finalizers_persist_one_winner(
    tmp_path: Path,
) -> None:
    """CR2.1: store-level flock admits one authoritative terminal writer."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def finalize(attempt: tuple[SpawnStatus, int, float]) -> tuple[bool, bool]:
        status, exit_code, duration = attempt
        outcome = finalize_spawn(
            runtime_root,
            spawn_id,
            status=status,
            exit_code=exit_code,
            origin="runner",
            duration_secs=duration,
        )
        return outcome.wrote, outcome.transitioned

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                finalize,
                (
                    ("succeeded", 0, 10.0),
                    ("failed", 1, 99.0),
                ),
            )
        )

    assert sorted(outcomes) == [(False, False), (True, True)]
    assert _state_revision(runtime_root, spawn_id) == 2
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status in {"succeeded", "failed"}
    assert row.duration_secs in {10.0, 99.0}


def test_cross_process_authoritative_finalizers_persist_one_winner(
    tmp_path: Path,
) -> None:
    """CR2.1: file-lock winner semantics hold across processes, not just threads."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    ctx = multiprocessing.get_context("spawn")
    ready_queue = ctx.Queue()
    result_queue = ctx.Queue()
    start_event = ctx.Event()
    processes = [
        ctx.Process(
            target=_finalize_in_subprocess,
            args=(
                runtime_root.as_posix(),
                spawn_id,
                "succeeded",
                0,
                10.0,
                ready_queue,
                start_event,
                result_queue,
            ),
        ),
        ctx.Process(
            target=_finalize_in_subprocess,
            args=(
                runtime_root.as_posix(),
                spawn_id,
                "failed",
                1,
                99.0,
                ready_queue,
                start_event,
                result_queue,
            ),
        ),
    ]

    for process in processes:
        process.start()

    for _ in processes:
        assert ready_queue.get(timeout=10) == "ready"
    start_event.set()

    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [result_queue.get(timeout=10) for _ in processes]
    row = get_spawn(runtime_root, spawn_id)

    assert sorted(outcomes) == [(False, False), (True, True)]
    assert _state_revision(runtime_root, spawn_id) == 2
    assert row is not None
    assert row.status in {"succeeded", "failed"}
    assert row.duration_secs in {10.0, 99.0}


def test_finalize_spawn_reconciler_writes_through_finalizing_row(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    assert mark_finalizing(runtime_root, spawn_id) is True

    outcome = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="reconciler",
        error="orphan_finalization",
    )

    assert outcome.transitioned is True
    assert outcome.wrote is True
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error == "orphan_finalization"


def test_finalize_spawn_reconciler_drops_when_row_already_terminal(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="runner",
        error="timeout",
    )
    outcome = finalize_spawn(
        runtime_root,
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="reconciler",
        duration_secs=999.0,
    )

    assert outcome.transitioned is False
    assert outcome.wrote is False
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error == "timeout"
    assert row.duration_secs is None


def test_exited_event_is_non_terminal_and_projects_process_exit(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=143,
        exited_at="2026-04-12T14:00:00Z",
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "running"
    assert row.exited_at == "2026-04-12T14:00:00Z"
    assert row.process_exit_code == 143
    assert row.exit_code is None
