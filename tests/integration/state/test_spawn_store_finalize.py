# qa-validated: test-suite-redesign

"""Finalization, mark_finalizing, and concurrency tests for the spawn store.

Covers: state-machine enforcement on mark_finalizing, authority/reconciler
write ordering, concurrent thread and cross-process finalize races.  CRUD
operations live in test_spawn_store_crud.py.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from meridian.lib.core.domain import (
    TERMINAL_SPAWN_STATUSES,
    SpawnStatus,
    TerminalSpawnStatus,
    TokenUsage,
)
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    mark_finalizing,
    mark_finalizing_with_snapshot,
    start_spawn,
)
from tests.support.process_race import run_spawn_race_or_skip


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

def test_finalize_propagates_concurrent_disappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def disappeared(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(spawn_id)

    monkeypatch.setattr("meridian.lib.state.spawn_store._write_state_locked", disappeared)

    with pytest.raises(FileNotFoundError):
        finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")


def test_mark_finalizing_propagates_concurrent_disappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def disappeared(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(spawn_id)

    monkeypatch.setattr("meridian.lib.state.spawn_store._write_state_locked", disappeared)

    with pytest.raises(FileNotFoundError):
        mark_finalizing_with_snapshot(runtime_root, spawn_id)


def _finalize_spawn_worker(
    runtime_root_str: str,
    spawn_id: str,
    status: TerminalSpawnStatus,
    exit_code: int,
    duration_secs: float,
) -> tuple[bool, bool]:
    outcome = finalize_spawn(
        Path(runtime_root_str),
        spawn_id,
        status=status,
        exit_code=exit_code,
        origin="runner",
        duration_secs=duration_secs,
    )
    return (outcome.wrote, outcome.transitioned)


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
        initial_status = "running" if start_status in TERMINAL_SPAWN_STATUSES else start_status
        spawn_id = str(
            start_spawn(
                runtime_root,
                chat_id=f"c-{start_status}",
                model="gpt-5.4",
                agent="coder",
                harness="codex",
                prompt="hello",
                status=initial_status,
            )
        )
        if start_status in TERMINAL_SPAWN_STATUSES:
            finalize_spawn(
                runtime_root,
                spawn_id,
                start_status,
                0,
                origin="runner",
            )
        assert mark_finalizing(runtime_root, spawn_id) is False
        row = get_spawn(runtime_root, spawn_id)
        assert row is not None
        assert row.status == start_status


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
    assert row.terminal is not None
    assert row.terminal.exit_code == 0
    assert row.terminal.error is None
    assert row.terminal.origin == "runner"
    assert row.terminal.duration_secs == 12.5


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
        usage=TokenUsage(total_cost_usd=5.0),
        error="loser",
    )

    assert first.wrote is True
    assert first.transitioned is True
    assert second.wrote is False
    assert second.transitioned is False
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.terminal is not None
    assert row.terminal.exit_code == 0
    assert row.terminal.duration_secs is None
    assert row.terminal.total_cost_usd is None
    assert row.terminal.error is None
    assert row.terminal.origin == "runner"


def test_cross_process_authoritative_finalizers_persist_one_winner(
    tmp_path: Path,
) -> None:
    """CR2.1: file-lock winner semantics hold across processes, not just threads."""
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    outcomes = run_spawn_race_or_skip(
        _finalize_spawn_worker,
        [
            (runtime_root.as_posix(), spawn_id, "succeeded", 0, 10.0),
            (runtime_root.as_posix(), spawn_id, "failed", 1, 99.0),
        ],
    )
    row = get_spawn(runtime_root, spawn_id)

    assert sorted(outcomes) == [(False, False), (True, True)]
    assert row is not None
    assert row.status in {"succeeded", "failed"}
    assert row.terminal is not None
    assert row.terminal.duration_secs in {10.0, 99.0}


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
    assert row.terminal is not None
    assert row.terminal.error == "orphan_finalization"
