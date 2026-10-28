"""The background repair pass is bounded and a failing row cannot stall it."""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.state import spawn_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.reaper import recover_crashed_launches

if TYPE_CHECKING:
    import pytest

_HOLD_LOCKS = """
import fcntl, sys
handles = []
for path in sys.argv[1:]:
    handle = open(path, "a")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handles.append(handle)
print("ready", flush=True)
sys.stdin.read()
"""


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
    runtime_root = tmp_path / "runtime"
    failing = _row(runtime_root, 1)  # dead, but its recovery raises
    live = [_row(runtime_root, n) for n in range(2, 72)]  # 70 live runners
    _row(runtime_root, 72)  # dead

    def fail_for_p1(root: Path, record: object, *args: object) -> None:
        if getattr(record, "id", "") == "p1":
            raise PermissionError(failing.as_posix())

    monkeypatch.setattr("meridian.lib.state.reaper._claim_reaper_cleanup", fail_for_p1)

    # Build the index up front: a first build inside a pass can outlast the
    # pass's query budget on a loaded machine, and the pass would skip.
    HistoryIndex(runtime_root).spawns(status={"running"})

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCKS, *(p.as_posix() for p in live)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"

        assert recover_crashed_launches(tmp_path, runtime_root) == ()
        assert _status(runtime_root, 72) == "running"  # beyond 64 attempts

        recovered = recover_crashed_launches(tmp_path, runtime_root)
        assert [row.id for row in recovered] == ["p72"]
        assert _status(runtime_root, 72) == "failed"
        assert _status(runtime_root, 1) == "running"
        assert _status(runtime_root, 2) == "running"
    finally:
        with suppress(OSError):
            holder.communicate(timeout=5)
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
