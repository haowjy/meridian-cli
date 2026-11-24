"""Pins how ``agy``'s cancel is read from its conversation database, against real SQLite."""

import sqlite3
from pathlib import Path

import pytest

from meridian.lib.harness.agy.idle import TurnState
from meridian.lib.harness.agy.state import busy_path, native_store

CONVERSATION = "c1"
NORMAL_END = bytes([0x08, 0x04])  # field 1 = 4, NO_TOOL_CALL
CANCEL = bytes([0x08, 0x02, 0x10, 0x01])  # field 1 = 2, USER_CANCELED, then another field


@pytest.fixture
def proc_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    proc = tmp_path / "proc"
    busy_path(proc, CONVERSATION).parent.mkdir(parents=True)
    return proc


def _database() -> Path:
    path = native_store().parent / "conversations" / f"{CONVERSATION}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(path: Path, *blobs: bytes) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS executor_metadata (idx INTEGER, data BLOB)")
        (last,) = db.execute("SELECT COALESCE(MAX(idx), -1) FROM executor_metadata").fetchone()
        db.executemany(
            "INSERT INTO executor_metadata VALUES (?, ?)",
            [(last + 1 + n, blob) for n, blob in enumerate(blobs)],
        )


def _touch(proc: Path) -> None:
    path = busy_path(proc, CONVERSATION)
    path.touch()
    path.write_text(path.read_text() + "x")  # a new mtime even on a coarse clock


def test_a_cancel_after_the_mark_was_touched_ends_the_turn(proc_dir: Path) -> None:
    database = _database()
    _append(database, NORMAL_END, CANCEL)  # history: an earlier Esc
    turn = TurnState(proc_dir, CONVERSATION)
    _touch(proc_dir)
    assert turn.state() == "running"
    assert turn.state() == "running"

    _append(database, NORMAL_END)  # a normal end is Stop's to report
    assert turn.state() == "running"
    _append(database, CANCEL)
    assert turn.state() == "cancelled"

    _touch(proc_dir)  # the next turn's PreInvocation
    assert turn.state() == "running"


def test_an_unreadable_baseline_never_ends_a_running_turn(proc_dir: Path) -> None:
    turn = TurnState(proc_dir, CONVERSATION)
    _touch(proc_dir)
    assert turn.state() == "running"  # no database yet: the baseline is unknown

    _append(_database(), NORMAL_END, CANCEL)  # past cancels appear once it is readable
    assert turn.state() == "running"  # this poll takes the baseline
    assert turn.state() == "running"
    _append(_database(), CANCEL)
    assert turn.state() == "cancelled"


def test_no_mark_is_no_turn(proc_dir: Path) -> None:
    assert TurnState(proc_dir, CONVERSATION).state() == "idle"
