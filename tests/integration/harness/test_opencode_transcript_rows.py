"""Native OpenCode transcript rows survive the provider boundary before rendering."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from meridian.lib.harness.opencode_transcript import (
    OpenCodeV2StorageTranscriptProvider,
    detect_opencode_db_schema,
    extract_last_assistant_report,
    iter_opencode_db_events,
    iter_opencode_db_session_events,
    iter_opencode_v2_db_events,
    opencode_db_any_session_exists,
    opencode_db_v2_session_exists,
    read_last_model,
)
from meridian.lib.harness.transcript import (
    iter_transcript_events,
    parse_transcript_events_with_prologues,
)
from meridian.lib.state.native_snapshot import TranscriptValidation
from tests.support.opencode_db import (
    write_opencode_db_session,
    write_opencode_db_session_with_parts,
    write_opencode_v2_db_session,
)


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


_SESSION = "ses_v2_fixture"
_V2_MESSAGES: list[tuple[str, dict[str, object]]] = [
    ("user", {"time": {"created": 1}, "text": "say hi"}),
    (
        "assistant",
        {
            "time": {"created": 2},
            "agent": "build",
            "model": {"id": "mimo-v2.5-free", "providerID": "opencode"},
            "content": [
                {"type": "text", "text": "hello"},
                {
                    "type": "tool",
                    "name": "shell",
                    "state": {
                        "status": "completed",
                        "input": {"command": "echo hi"},
                        "content": [{"type": "text", "text": "hi\n"}],
                    },
                },
            ],
        },
    ),
    ("idle", {"time": {"created": 3}, "outcome": "succeeded"}),
]


def _no_json_events(
    path: Path,
    *,
    current: Callable[[], bool] | None = None,
    validation: TranscriptValidation | None = None,
) -> Iterator[dict[str, object]]:
    del path, current, validation
    return iter(())


def _write_v2(
    tmp_path: Path,
    *,
    session_id: str = _SESSION,
    messages: list[tuple[str, dict[str, object]]] | None = None,
    model: dict[str, object] | None = None,
    parent_id: str | None = None,
    idle_outcome: str | None = None,
) -> Path:
    path = tmp_path / "opencode.db"
    write_opencode_v2_db_session(
        db_path=path,
        session_id=session_id,
        messages=_V2_MESSAGES if messages is None else messages,
        model={"id": "mimo-v2.5-free", "providerID": "opencode"} if model is None else model,
        parent_id=parent_id,
        idle_outcome=idle_outcome,
    )
    return path


def test_detect_schema_by_table_presence(tmp_path: Path) -> None:
    v2 = _write_v2(tmp_path)
    assert detect_opencode_db_schema(v2) == "sqlite_v2"

    v1 = tmp_path / "v1.db"
    write_opencode_db_session(db_path=v1, session_id="s", messages=[])
    assert detect_opencode_db_schema(v1) == "sqlite_v1"

    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty):
        pass
    assert detect_opencode_db_schema(empty) is None
    assert detect_opencode_db_schema(tmp_path / "missing.db") is None


def test_v2_rows_read_into_transcript_types(tmp_path: Path) -> None:
    path = _write_v2(tmp_path, parent_id="ses_parent", idle_outcome="succeeded")
    events = list(iter_opencode_v2_db_events(session_id=_SESSION, db_path=path))

    assert [event["record"] for event in events] == ["opencode.transcript.v2"] * 4
    assert [event["type"] for event in events] == ["session", "user", "assistant", "idle"]
    session_row = events[0]["data"]
    assert isinstance(session_row, dict)
    assert session_row["id"] == _SESSION
    assert session_row["parent_id"] == "ses_parent"
    assert session_row["idle_outcome"] == "succeeded"

    parsed = parse_transcript_events_with_prologues(events)
    messages = [message for segment in parsed.segments for message in segment]
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "assistant",
        "user",
    ]


def test_extract_last_assistant_report_from_v2_rows(tmp_path: Path) -> None:
    path = _write_v2(tmp_path)
    events = iter_opencode_v2_db_events(session_id=_SESSION, db_path=path)
    assert extract_last_assistant_report(events) == "hello"


def test_extract_last_assistant_report_v2_without_assistant_text_is_none(
    tmp_path: Path,
) -> None:
    path = _write_v2(tmp_path, messages=[("user", {"text": "say hi"})])
    events = iter_opencode_v2_db_events(session_id=_SESSION, db_path=path)
    assert extract_last_assistant_report(events) is None


def test_v2_unknown_message_type_sets_rendering_reason(tmp_path: Path) -> None:
    path = _write_v2(
        tmp_path,
        messages=[
            ("user", {"text": "hi"}),
            ("summary", {"text": "condensed"}),
        ],
    )
    events = list(iter_opencode_v2_db_events(session_id=_SESSION, db_path=path))
    assert [event["type"] for event in events] == ["session", "user", "summary"]

    parsed = parse_transcript_events_with_prologues(events)
    assert parsed.rendering_reason is not None


def test_provider_selection_is_schema_aware(tmp_path: Path) -> None:
    storage_home = tmp_path / "opencode2"
    v2_db = storage_home / "opencode.db"
    write_opencode_v2_db_session(db_path=v2_db, session_id=_SESSION, messages=_V2_MESSAGES)
    session_file = storage_home / "storage" / "session" / f"{_SESSION}.json"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n")

    provider = OpenCodeV2StorageTranscriptProvider(iter_json_events=_no_json_events)
    assert provider.supports(session_file) is True
    records = [event.get("record") for event in iter_transcript_events(session_file)]
    assert records and set(records) == {"opencode.transcript.v2"}

    v1_db = storage_home / "v1" / "opencode.db"
    write_opencode_db_session(db_path=v1_db, session_id=_SESSION, messages=[])
    v1_file = v1_db.parent / "storage" / "session" / f"{_SESSION}.json"
    v1_file.parent.mkdir(parents=True)
    v1_file.write_text("{}\n")
    assert provider.supports(v1_file) is False


def test_missing_table_and_fresh_db_are_graceful(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty):
        pass

    assert list(iter_opencode_v2_db_events(session_id=_SESSION, db_path=empty)) == []
    assert read_last_model(_SESSION, db_path=empty) is None
    assert opencode_db_v2_session_exists(session_id=_SESSION, db_path=empty) is False
    assert opencode_db_any_session_exists(session_id=_SESSION, db_path=empty) is False

    no_messages = _write_v2(tmp_path, messages=[])
    with sqlite3.connect(no_messages) as connection:
        connection.execute("DROP TABLE session_message")
    assert list(iter_opencode_v2_db_events(session_id=_SESSION, db_path=no_messages)) == []
    assert list(iter_opencode_db_session_events(session_id=_SESSION, db_path=no_messages)) == []

    missing_session = _write_v2(tmp_path / "other")
    assert list(iter_opencode_v2_db_events(session_id="ses_nope", db_path=missing_session)) == []
