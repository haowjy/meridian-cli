# qa-validated: test-suite-redesign

"""Finalization, mark_finalizing, and concurrency tests for the spawn store.

Covers: state-machine enforcement on mark_finalizing, authority/reconciler
write ordering, concurrent thread and cross-process finalize races.  CRUD
operations live in test_spawn_store_crud.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.domain import (
    TerminalSpawnStatus,
    TokenUsage,
)
from meridian.lib.state.spawn.repository import Applied, Declined
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    start_spawn,
)
from tests.support import legacy_rows
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
        ).spawn_id
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
    legacy_rows.mark_finalizing(runtime_root, spawn_id)

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
