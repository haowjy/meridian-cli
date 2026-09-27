"""Each history schema owns its projection namespace; older builds keep theirs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from meridian.lib.state import history_changes, spawn_store
from meridian.lib.state.history_changes import (
    SCHEMA_VERSION,
    DirtySource,
    HistoryChanges,
    HistorySource,
)
from meridian.lib.state.history_index import HistoryIndex, HistoryIndexIncomplete

# 0.6.7 layout: one unversioned file, queue, latch and lock set.
_LEGACY_GENERATION = "19c39f11-bd07-47be-b2fb-4d18f3e7e945"
_LEGACY_LOCKS = ("history-catchup.lock", "history-database.lock", "history-markers.lock")
# Tables of a schema-2 index as written by `uvx --from meridian-cli==0.6.7 meridian`.
_LEGACY_DDL = """
CREATE TABLE meta (version INTEGER NOT NULL, generation TEXT NOT NULL, build TEXT NOT NULL);
CREATE TABLE previews ("key" TEXT NOT NULL PRIMARY KEY, history_id TEXT, archive_digest TEXT,
    value TEXT NOT NULL);
CREATE TABLE records (history_id TEXT NOT NULL PRIMARY KEY, local_id TEXT, chat TEXT,
    owner TEXT, parent TEXT, work TEXT, status TEXT NOT NULL, kind TEXT NOT NULL,
    started TEXT NOT NULL, activity TEXT NOT NULL, active INTEGER NOT NULL, archive_id TEXT,
    record_json TEXT NOT NULL);
CREATE UNIQUE INDEX loose_alias ON records (local_id) WHERE archive_id IS NULL;
CREATE TABLE locations (source_id TEXT NOT NULL PRIMARY KEY, history_id TEXT NOT NULL,
    kind TEXT NOT NULL, ordinal INTEGER NOT NULL, activity TEXT NOT NULL,
    record_json TEXT NOT NULL, receipt_json TEXT, portable_digest TEXT);
CREATE TABLE archive_heads (history_id TEXT NOT NULL PRIMARY KEY,
    portable_digest TEXT NOT NULL);
CREATE TABLE aliases (source_id TEXT NOT NULL, alias TEXT NOT NULL, kind TEXT NOT NULL,
    history_id TEXT NOT NULL, PRIMARY KEY (source_id, alias, kind, history_id));
CREATE TABLE sessions (chat TEXT NOT NULL, generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL, stopped TEXT, activity TEXT NOT NULL, history_id TEXT,
    record_json TEXT NOT NULL, PRIMARY KEY (chat, generation));
CREATE TABLE work_chats (work TEXT NOT NULL, chat TEXT NOT NULL, PRIMARY KEY (work, chat));
CREATE TABLE cursors (source TEXT NOT NULL PRIMARY KEY, inode TEXT, extent INTEGER, tail TEXT);
"""


def _legacy_projection(root: Path) -> dict[str, bytes]:
    """Write what 0.6.7 leaves behind: a schema-2 index, its queue and its latch."""
    directory = root / "history-index"
    (directory / "pending").mkdir(parents=True)
    with sqlite3.connect(directory / "history.sqlite3") as db:
        db.executescript(_LEGACY_DDL)
        db.execute("INSERT INTO meta VALUES (2, ?, 'legacy-build')", (_LEGACY_GENERATION,))
    db.close()
    (directory / "pending" / "GENERATION").write_text(_LEGACY_GENERATION + "\n")
    marker = DirtySource(
        source=HistorySource(kind="spawn", key="p1"),
        token="00000000-0000-4000-8000-000000000001",
    )
    (directory / "pending" / marker.source.name).write_text(marker.model_dump_json() + "\n")
    (root / "history-index-init-failure.json").write_text(
        json.dumps(
            {
                "format": 1,
                "target_schema": 2,
                "generation": _LEGACY_GENERATION,
                "code": "authority",
                "reason": "legacy failure",
                "failed_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    return _snapshot(root)


def _snapshot(root: Path) -> dict[str, bytes]:
    legacy = [
        root / "history-index" / "history.sqlite3",
        root / "history-index-init-failure.json",
        *sorted((root / "history-index" / "pending").iterdir()),
    ]
    return {str(path.relative_to(root)): path.read_bytes() for path in legacy}


def _assert_legacy_untouched(root: Path, before: dict[str, bytes]) -> None:
    assert _snapshot(root) == before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (root / "history-index" / f"history.sqlite3{suffix}").exists()
    for name in _LEGACY_LOCKS:
        assert not (root / "locks" / name).exists()


def _finished_spawn(root: Path) -> str:
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    return key


def test_every_projection_path_is_named_by_the_schema(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)
    assert index.path.name == f"history-v{SCHEMA_VERSION}.sqlite3"

    monkeypatch.setattr(history_changes, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    changes = HistoryChanges(tmp_path)
    owned = {
        index.path,
        index.stage,
        index.catchup_lock,
        index.database_lock,
        index.failure_path,
        changes.directory,
        changes.marker_lock,
    }
    assert all(f"-v{SCHEMA_VERSION + 1}" in path.name for path in owned)
    # Authority is shared by every build; its gate is not versioned.
    assert changes.mutation_lock.name == "history-mutation.lock"


def test_fresh_build_leaves_an_older_schema_projection_byte_identical(tmp_path: Path) -> None:
    before = _legacy_projection(tmp_path)
    key = _finished_spawn(tmp_path)

    index = HistoryIndex(tmp_path)
    assert [row.id for row in index.spawns()] == [key]
    assert index.inspect().baseline == "current"
    assert index.inspect().generation != _LEGACY_GENERATION
    index.rebuild(reset=True)

    _assert_legacy_untouched(tmp_path, before)


def test_initialization_latch_is_scoped_to_this_schema_file(tmp_path: Path) -> None:
    _legacy_projection(tmp_path)
    # A pre-fix build wrote schema-6 failures to the unversioned latch.
    (tmp_path / "history-index-init-failure.json").write_text(
        json.dumps(
            {
                "format": 1,
                "target_schema": SCHEMA_VERSION,
                "generation": None,
                "code": "authority",
                "reason": "stale latch against history.sqlite3",
                "failed_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    before = _snapshot(tmp_path)
    key = _finished_spawn(tmp_path)
    index = HistoryIndex(tmp_path)
    assert index.inspect().baseline == "absent"
    assert [row.id for row in index.spawns()] == [key]
    _assert_legacy_untouched(tmp_path, before)

    state = tmp_path / "spawns" / key / "state.json"
    state.write_text("[]\n")
    index.path.unlink()
    with pytest.raises(HistoryIndexIncomplete, match="initialization failed"):
        index.spawns()
    assert index.failure_path.name == f"history-index-init-failure-v{SCHEMA_VERSION}.json"
    assert index.failure_path.exists()
    _assert_legacy_untouched(tmp_path, before)


def test_active_rows_and_session_log_follow_writers_that_mark_another_queue(
    tmp_path: Path,
) -> None:
    """A runner from an older build finishes after upgrade without marking this queue."""
    from meridian.lib.state import session_store

    key = spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    session_store.start_session(
        tmp_path, harness="codex", harness_session_id="", model="test", chat_id="c9"
    )
    index = HistoryIndex(tmp_path)
    assert [row.status for row in index.spawns()] == ["running"]

    # Simulate the older writer: authority changes, only its own queue is marked.
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    session_store.stop_session(tmp_path, "c9")
    changes = HistoryChanges(tmp_path)
    for path in changes.directory.glob("*.json"):
        path.unlink()

    assert [row.status for row in index.spawns()] == ["succeeded"]
    stopped = [row for row in index.sessions() if row.chat_id == "c9"]
    assert stopped and stopped[0].stopped_at is not None


def test_new_build_first_command_leaves_a_schema_2_runtime_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """Overlap regression: 0.6.7 runners must keep reading their schema-2 index."""
    home = tmp_path / "home"
    root = tmp_path / "runtime"
    project = tmp_path / "project"
    for path in (home, root, project):
        path.mkdir()
    before = _legacy_projection(root)
    key = _finished_spawn(root)
    env = {
        **os.environ,
        "HOME": str(home),
        "MERIDIAN_HOME": str(home / ".meridian"),
        "_MERIDIAN_RUNTIME_DIR": str(root),
    }
    for name in ("MERIDIAN_SPAWN_ID", "_MERIDIAN_DEPTH", "MERIDIAN_CHAT_ID"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "meridian", "spawn", "list", "--all"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert key in result.stdout

    _assert_legacy_untouched(root, before)
    assert HistoryIndex(root).inspect().baseline == "current"
