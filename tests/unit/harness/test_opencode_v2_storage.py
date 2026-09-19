"""OpenCode V2 native storage read path: rows, last model, graceful absence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from meridian.lib.harness.opencode_transcript import (
    OpenCodeV2StorageTranscriptProvider,
    detect_opencode_db_schema,
    extract_last_assistant_report,
    iter_opencode_db_session_events,
    iter_opencode_v2_db_events,
    opencode_db_any_session_exists,
    opencode_db_v2_session_exists,
    read_last_model,
    resolve_opencode_db_path,
)
from meridian.lib.harness.transcript import (
    iter_transcript_events,
    parse_transcript_events_with_prologues,
)
from meridian.lib.state.native_snapshot import TranscriptValidation
from tests.support.opencode_db import (
    write_opencode_db_session,
    write_opencode_v2_db_session,
)

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


def test_resolve_opencode_db_path_precedence() -> None:
    assert resolve_opencode_db_path({"OPENCODE_DB": "/tmp/custom.db"}) == Path("/tmp/custom.db")
    assert resolve_opencode_db_path({"OPENCODE_DB": ":memory:"}) == Path(":memory:")
    assert resolve_opencode_db_path(
        {"OPENCODE_DB": "nested/iso.db", "XDG_DATA_HOME": "/data"}
    ) == Path("/data/opencode/nested/iso.db")
    assert resolve_opencode_db_path(
        {"OPENCODE_DB": "nested/iso.db", "OPENCODE_HOME": "/custom/opencode"}
    ) == Path("/custom/opencode/nested/iso.db")
    assert resolve_opencode_db_path({"XDG_DATA_HOME": "/data"}) == Path(
        "/data/opencode/opencode.db"
    )
    assert resolve_opencode_db_path({"HOME": "/home/u"}) == Path(
        "/home/u/.local/share/opencode/opencode.db"
    )


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
    assert messages[0].content == "say hi"
    assert messages[1].content == "hello"
    assert messages[2].tool_call is not None
    assert messages[2].tool_call.name == "bash"
    assert messages[3].is_tool_result is True
    assert parsed.rendering_reason is None


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
    write_opencode_v2_db_session(
        db_path=v2_db, session_id=_SESSION, messages=_V2_MESSAGES
    )
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


def test_read_last_model_v2_and_v1_fallback(tmp_path: Path) -> None:
    v2 = _write_v2(tmp_path)
    assert read_last_model(_SESSION, db_path=v2) == "opencode/mimo-v2.5-free"
    assert read_last_model("ses_missing", db_path=v2) is None

    v1 = tmp_path / "v1.db"
    write_opencode_db_session(db_path=v1, session_id=_SESSION, messages=[])
    with sqlite3.connect(v1) as connection:
        connection.execute("ALTER TABLE session ADD COLUMN model TEXT")
        connection.execute(
            "UPDATE session SET model=?",
            (json.dumps({"id": "gpt-5.5", "providerID": "openai"}),),
        )
    assert read_last_model(_SESSION, db_path=v1) == "openai/gpt-5.5"


def test_v1_session_in_v2_migrated_db_reads_last_model(tmp_path: Path) -> None:
    path = _write_v2(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                model TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO session (id, time_created, time_updated, model) VALUES (?, 1, 1, ?)",
            ("ses_legacy", json.dumps({"id": "legacy-model", "providerID": "acme"})),
        )
    assert read_last_model("ses_legacy", db_path=path) == "acme/legacy-model"
    assert detect_opencode_db_schema(path) == "sqlite_v2"


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
