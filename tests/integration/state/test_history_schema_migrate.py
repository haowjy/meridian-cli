"""Compatible history indexes migrate in place; rebuild stays recovery."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection  # noqa: TC002

from meridian.lib.state import history_index, spawn_store
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_index import HistoryIndex, SchemaStep


def _add_probe_note(db: Connection) -> None:
    db.exec_driver_sql("ALTER TABLE records ADD COLUMN probe_note TEXT")


def _enable_v3_expand(monkeypatch: pytest.MonkeyPatch, apply=_add_probe_note) -> None:
    monkeypatch.setattr(history_index, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(history_index, "SCHEMA_STEPS", (SchemaStep(2, 3, "expand", apply),))


def _v2_index(tmp_path: Path) -> tuple[HistoryIndex, str, str]:
    key = spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    index = HistoryIndex(tmp_path)
    index.rebuild()
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE meta SET version=2")
        history_id = str(db.execute("SELECT history_id FROM records").fetchone()[0])
        db.execute("INSERT INTO previews VALUES ('probe', ?, NULL, 'cached')", (history_id,))
        db.execute("INSERT INTO cursors VALUES ('sessions', '1:2', 12, 'tail')")
    return index, key, history_id


def _record_columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(records)")}


def test_expand_migrate_keeps_rows_and_adds_column(tmp_path: Path, monkeypatch) -> None:
    index, key, history_id = _v2_index(tmp_path)
    build = index.inspect().build
    _enable_v3_expand(monkeypatch)
    status = index.inspect()
    assert status.baseline == "outdated"
    assert status.upgrade == "migrate"
    assert status.reason == "in-place migrate (expand)"
    assert [row.id for row in index.spawns()] == [key]
    after = index.inspect()
    assert after.baseline == "current" and after.schema == 3 and after.build == build
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT history_id FROM records").fetchone()[0] == history_id
        preview = db.execute("SELECT value FROM previews WHERE key='probe'").fetchone()[0]
        cursor = db.execute("SELECT tail FROM cursors WHERE source='sessions'").fetchone()[0]
        assert preview == "cached" and cursor == "tail"
    assert "probe_note" in _record_columns(index.path)


def test_expand_migrate_rolls_back_before_commit(tmp_path: Path, monkeypatch) -> None:
    def crash(db: Connection) -> None:
        db.exec_driver_sql("ALTER TABLE records ADD COLUMN probe_note TEXT")
        raise RuntimeError("crash before commit")

    index, _, _ = _v2_index(tmp_path)
    _enable_v3_expand(monkeypatch, apply=crash)
    with pytest.raises(RuntimeError, match="crash before commit"):
        index.initialize(deadline=time.monotonic() + 15)
    assert index.inspect().schema == 2
    assert "probe_note" not in _record_columns(index.path)


def test_expand_migrate_still_drains_dirty_markers(tmp_path: Path, monkeypatch) -> None:
    index, key, _ = _v2_index(tmp_path)
    changes = HistoryChanges(tmp_path)
    changes.mark(HistorySource(kind="spawn", key=key))
    _, pending = changes.inspect()
    assert pending
    _enable_v3_expand(monkeypatch)
    assert [row.id for row in index.spawns()] == [key]
    _, pending = changes.inspect()
    assert pending == ()


def test_corrupt_index_is_rebuilt_not_migrated(tmp_path: Path, monkeypatch) -> None:
    index, key, _ = _v2_index(tmp_path)
    index.path.write_bytes(b"not a database")
    for suffix in ("-wal", "-shm"):
        Path(str(index.path) + suffix).unlink(missing_ok=True)
    _enable_v3_expand(monkeypatch)
    assert index.inspect().baseline == "corrupt"
    with pytest.raises(history_index.HistoryIndexIncomplete, match="corrupt"):
        index.initialize(deadline=time.monotonic() + 15)
    assert index.rebuild().complete
    assert index.inspect().baseline == "current"
    assert [row.id for row in index.spawns()] == [key]
    assert "probe_note" not in _record_columns(index.path)


def test_newer_schema_stays_incompatible(tmp_path: Path, monkeypatch) -> None:
    index, _, _ = _v2_index(tmp_path)
    with sqlite3.connect(index.path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("UPDATE meta SET version=999")
    _enable_v3_expand(monkeypatch)
    status = index.inspect()
    assert status.baseline == "incompatible"
    assert status.upgrade is None
    with pytest.raises(history_index.HistoryIndexIncomplete, match="incompatible"):
        index.spawns()
