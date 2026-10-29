"""The background repair pass is bounded and a failing row cannot stall it."""

from __future__ import annotations

import fcntl
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.state import spawn_store
from meridian.lib.state.reaper import recover_crashed_launches

if TYPE_CHECKING:
    import pytest


def _row(runtime_root: Path, number: int) -> Path:
    spawn_id = f"p{number}"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="tester",
        harness="claude",
        kind="child",
        prompt="hello",
        started_at="2000-01-01T00:00:00Z",
        status="running",
    )
    lock = runtime_root / "spawns" / spawn_id / "launch.lock"
    lock.touch()
    return lock


def _status(runtime_root: Path, number: int) -> str:
    row = spawn_store.get_spawn(runtime_root, f"p{number}")
    assert row is not None
    return row.status


def test_dead_row_behind_live_rows_and_a_failing_row_is_reached_in_two_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setattr("meridian.lib.state.reaper._MAX_LOCK_ATTEMPTS_PER_PASS", 4)
    runtime_root = tmp_path / "runtime"
    failing = _row(runtime_root, 1)  # dead, but its recovery raises
    live = [_row(runtime_root, n) for n in range(2, 6)]  # 4 live runners
    _row(runtime_root, 6)  # dead

    def fail_for_p1(root: Path, record: object, *args: object) -> None:
        if getattr(record, "id", "") == "p1":
            raise PermissionError(failing.as_posix())

    monkeypatch.setattr("meridian.lib.state.reaper._claim_reaper_cleanup", fail_for_p1)

    with ExitStack() as holders:
        for path in live:
            handle = holders.enter_context(path.open("a"))
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        assert recover_crashed_launches(tmp_path, runtime_root) == ()
        assert _status(runtime_root, 6) == "running"  # beyond 4 attempts

        recovered = recover_crashed_launches(tmp_path, runtime_root)
        assert [row.id for row in recovered] == ["p6"]
        assert _status(runtime_root, 6) == "failed"
        assert _status(runtime_root, 1) == "running"
        assert _status(runtime_root, 2) == "running"
