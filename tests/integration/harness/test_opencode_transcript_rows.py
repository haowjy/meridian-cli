"""Native OpenCode transcript rows survive the provider boundary before rendering."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from meridian.lib.harness.opencode_transcript import iter_opencode_db_events
from meridian.lib.harness.transcript import parse_transcript_events_with_prologues
from meridian.lib.harness.transcript_preview import PreviewAccumulator
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state import spawn_store
from meridian.lib.state.history import ingest_portable_history
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.retention_archive import iter_archived_events
from tests.support.history import written_events as iter_history_events
from tests.support.opencode_db import write_opencode_db_session_with_parts


def test_raw_rows_preserve_unknown_material_columns_and_orphan_parts(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=path,
        session_id="s",
        messages=[("future-role", {"unknown": [1, 2]}, [{"type": "future-part", "data": "kept"}])],
    )
    raw = '{ "role" : "future-role", "unknown": [1, 2] }'
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE session ADD COLUMN extra BLOB")
        db.execute("UPDATE session SET extra=?", (b"\x00\xff",))
        db.execute("UPDATE message SET data=?", (raw,))
        db.execute(
            "INSERT INTO part VALUES ('orphan','missing','s',9,10,?)",
            ('{"type":"text","text":"orphan content"}',),
        )
        db.execute("INSERT INTO session(id,time_created,time_updated) VALUES ('other',1,1)")
        db.execute("INSERT INTO message VALUES ('missing','other',1,1,?)", ('{"role":"user"}',))
        expected_message = dict(
            zip(
                [c[1] for c in db.execute("PRAGMA table_info(message)")],
                db.execute("SELECT * FROM message WHERE session_id='s'").fetchone(),
                strict=True,
            )
        )
    events = list(iter_opencode_db_events(session_id="s", db_path=path))
    assert [event["table"] for event in events] == ["session", "message", "part"]
    assert events[0]["row"]["extra"] == {"sqlite_type": "blob", "base64": "AP8="}
    assert events[1]["row"] == expected_message
    assert events[1]["row"]["data"] == raw
    assert events[1]["parts"][0]["id"] == "s_prt_0_0"
    assert events[2]["row"]["id"] == "orphan"
    # The same raw dialect remains interpretable after JSON serialization/retention.
    parsed = parse_transcript_events_with_prologues(json.loads(json.dumps(events)))
    assert parsed.rendering_reason
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="question", harness="opencode", model="test", agent="coder"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(root, key, iter(events))
    retained = list(iter_history_events(root / "spawns" / key / "history.jsonl"))
    assert [event["payload"] for event in retained] == events
    record = spawn_store.get_spawn(root, key)
    assert record is not None and record.history_id is not None
    archived = archive_history(root, destination=tmp_path / "archives", refs=(key,), apply=True)
    assert archived.reclaimed
    exported = list(iter_archived_events(Path(archived.archives[0]), record.history_id))
    assert [event["payload"] for event in exported] == events


@pytest.mark.parametrize("failure", ["missing-file", "missing-session", "missing-table"])
def test_unavailable_database_is_not_successful_empty(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "opencode.db"
    if failure != "missing-file":
        write_opencode_db_session_with_parts(
            db_path=path, session_id="s" if failure == "missing-table" else "other", messages=[]
        )
        if failure == "missing-table":
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE part")
    with pytest.raises((FileNotFoundError, ValueError, sqlite3.Error)):
        list(iter_opencode_db_events(session_id="s", db_path=path))
    if failure == "missing-file":
        assert not path.exists()


def test_valid_empty_has_positive_session_identity(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(db_path=path, session_id="s", messages=[])
    events = list(iter_opencode_db_events(session_id="s", db_path=path))
    assert len(events) == 1 and events[0]["row"]["id"] == "s"
    assert parse_transcript_events_with_prologues(events).rendering_reason is None


def test_raw_read_is_one_snapshot_including_session_and_parts(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=path,
        session_id="s",
        messages=[("assistant", {}, [{"type": "text", "text": "before"}])],
    )
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    events = iter_opencode_db_events(session_id="s", db_path=path)
    session = next(events)
    assert session["table"] == "session"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE part SET data=?", ('{"type":"text","text":"after"}',))
    rest = list(events)
    assert json.loads(rest[0]["parts"][0]["data"])["text"] == "before"


@pytest.mark.parametrize(
    "compaction", [{"mode": "compaction", "agent": "compaction"}, {"summary": True}]
)
def test_raw_projection_keeps_compaction_and_checkpoint_setup(
    tmp_path: Path, compaction: dict[str, object]
) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=path,
        session_id="s",
        messages=[
            ("user", {"system": "initial setup"}, [{"type": "text", "text": "question"}]),
            ("assistant", {}, []),
            ("assistant", {}, [{"type": "text", "text": "first answer"}]),
            (
                "assistant",
                compaction,
                [{"type": "text", "text": "handoff"}],
            ),
            ("assistant", {}, [{"type": "text", "text": "second answer"}]),
        ],
    )
    events = list(iter_opencode_db_events(session_id="s", db_path=path))
    assert events[0].get("record") == "opencode.transcript"
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.total_compactions == 1
    assert parsed.segment_setups == ("initial setup", "handoff")
    accumulator = PreviewAccumulator()
    for event in events:
        accumulator = PreviewAccumulator(accumulator.preview)
        accumulator.feed(event)
    assert accumulator.preview.setup == "handoff"
    assert [message.content for message in accumulator.preview.messages] == ["second answer"]


@pytest.mark.parametrize(
    "bad_part",
    [
        {"type": "text", "text": 42},
        {"type": {}},
        {"type": []},
        {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": 42, "output": "out"},
        },
        {"type": "future-material", "text": "unknown"},
    ],
)
def test_raw_projection_preserves_supported_content_beside_diagnostic(
    tmp_path: Path, bad_part: dict[str, object]
) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=path,
        session_id="s",
        messages=[
            ("assistant", {}, [{"type": "text", "text": "kept"}, bad_part]),
        ],
    )
    events = list(iter_opencode_db_events(session_id="s", db_path=path))
    assert json.loads(events[1]["parts"][1]["data"]) == bad_part
    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.rendering_reason and parsed.segments[0][0].content == "kept"


def test_checkpoint_does_not_borrow_later_user_system_setup(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=path,
        session_id="s",
        messages=[
            ("user", {}, [{"type": "text", "text": "first"}]),
            ("user", {"system": "later setup"}, [{"type": "text", "text": "second"}]),
        ],
    )
    accumulator = PreviewAccumulator()
    for event in iter_opencode_db_events(session_id="s", db_path=path):
        accumulator = PreviewAccumulator(accumulator.preview)
        accumulator.feed(event)
    assert accumulator.preview.setup is None
    assert [message.content for message in accumulator.preview.messages] == ["first", "second"]
