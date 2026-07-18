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
from meridian.lib.state.spawn.repository import Applied, Declined, Missing
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    mark_finalizing,
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


def test_mark_finalizing_reports_concurrent_disappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def disappeared(*_args: object, **_kwargs: object) -> Missing:
        return Missing()

    monkeypatch.setattr("meridian.lib.state.spawn_store._write_state_locked", disappeared)

    assert isinstance(mark_finalizing(runtime_root, spawn_id), Missing)


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
    return (
        isinstance(outcome, Applied),
        isinstance(outcome, Applied) and outcome.before.status not in {
            "succeeded", "failed", "cancelled", "timed_out"
        },
    )


def test_mark_finalizing_state_machine_enforces_running_only(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    running_spawn_id = _start_test_spawn(runtime_root)

    assert isinstance(mark_finalizing(runtime_root, running_spawn_id), Applied)
    row = get_spawn(runtime_root, running_spawn_id)
    assert row is not None
    assert row.status == "finalizing"
    assert isinstance(mark_finalizing(runtime_root, "p-missing"), Missing)

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
        assert isinstance(mark_finalizing(runtime_root, spawn_id), Declined)
        row = get_spawn(runtime_root, spawn_id)
        assert row is not None
        assert row.status == start_status


def test_mark_finalizing_concurrent_race_only_one_writer_wins(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    def attempt(_unused: int) -> bool:
        return isinstance(mark_finalizing(runtime_root, spawn_id), Applied)

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
    assert isinstance(reconciler_outcome, Applied)
    assert isinstance(runner_outcome, Applied)
    assert runner_outcome.before.status == "failed"
    assert runner_outcome.after.status == "succeeded"

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

    assert isinstance(first, Applied)
    assert isinstance(second, Declined)
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
    assert isinstance(mark_finalizing(runtime_root, spawn_id), Applied)

    outcome = finalize_spawn(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=1,
        origin="reconciler",
        error="orphan_finalization",
    )

    assert isinstance(outcome, Applied)
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.terminal is not None
    assert row.terminal.error == "orphan_finalization"
