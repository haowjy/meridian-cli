"""The disposable projection must replay the same accepted bindings as authority."""

import json
import sqlite3
from pathlib import Path

import pytest

from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.session_store import list_session_generations


def _append(root: Path, *events: dict) -> None:
    root.mkdir(exist_ok=True)
    with (root / "sessions.jsonl").open("a") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    HistoryChanges(root).mark(HistorySource(kind="sessions"))


def _start(generation: str, **fields: object) -> dict:
    return {
        "event": "start",
        "chat_id": "c1",
        "harness": "codex",
        "model": "test",
        "harness_session_id": "accepted",
        "native_store": "/native",
        "session_instance_id": generation,
        "started_at": "2026-01-01T00:00:00Z",
        **fields,
    }


def _generation_records(index: HistoryIndex) -> list[dict]:
    index.catch_up()
    with sqlite3.connect(index.path) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT record_json FROM sessions ORDER BY chat, ordinal")
        ]


@pytest.mark.parametrize("native_id", [None, "conflicting"])
def test_resumed_generation_binding_matches_authority(tmp_path: Path, native_id: str | None):
    _append(tmp_path, _start("g1"))
    index = HistoryIndex(tmp_path)
    index.rebuild()
    _append(tmp_path, _start("g2", harness_session_id=native_id, native_store=None))
    assert _generation_records(index) == [
        row.model_dump(mode="json") for row in list_session_generations(tmp_path)
    ]


def _rows(index: HistoryIndex) -> dict[str, list[tuple]]:
    index.catch_up()
    with sqlite3.connect(index.path) as db:
        return {
            table: sorted(db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in ("records", "sessions", "session_chats", "aliases", "work_chats")
        }


def test_incremental_replay_survives_restart_and_matches_cold(tmp_path: Path):
    from meridian.lib.state.session_store import list_all_session_records

    batches = [
        [_start(" a ", harness_session_id=None, native_store=None)],
        [
            {
                "event": "update",
                "chat_id": "c1",
                "session_instance_id": "a",
                "harness_session_id": "accepted",
                "native_store": "/native",
            }
        ],
        [_start("", harness_session_id=None, native_store=None)],
        [
            {
                "event": "stop",
                "chat_id": "c1",
                "session_instance_id": "",
                "stopped_at": "2026-01-02T00:00:00Z",
            },
            _start(""),
        ],
        [
            _start("bad", harness_session_id="conflict"),
            {
                "event": "update",
                "chat_id": "c1",
                "session_instance_id": " a ",
                "active_work_id": "late-work",
                "harness_session_id": "accepted",
            },
        ],
    ]
    for batch in batches:
        _append(tmp_path, *batch)
        index = HistoryIndex(tmp_path)
        _rows(index)
        assert index.sessions() == list_all_session_records(tmp_path)
    incremental = _rows(HistoryIndex(tmp_path))
    HistoryIndex(tmp_path).rebuild()
    assert _rows(HistoryIndex(tmp_path)) == incremental


def test_generated_histories_index_bindings_match_authority(tmp_path: Path, monkeypatch):
    """Run P5's unchanged 1,000-seed generator and golden, checking the index too."""
    from collections import Counter
    from datetime import UTC, datetime

    from sqlalchemy import create_engine

    from meridian.lib.state.history_index import INDEX_SCHEMA
    from tests.integration.state import test_generated_session_fold as golden

    original = golden.s.list_session_generations
    projected_root = tmp_path / "projection"
    projected_root.mkdir()
    engine = create_engine("sqlite://")
    INDEX_SCHEMA.create_all(engine)

    def check_projection(root: Path):
        expected = original(root)
        # P5 uses integer strings for time; only normalize these for the metadata
        # index's ISO-time columns. Bindings and generation spellings stay exact.
        events = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
        for event in events:
            timed = event.get("record", event)
            for name in ("started_at", "stopped_at"):
                if timed.get(name) is not None:
                    timed[name] = datetime.fromtimestamp(int(timed[name]), UTC).isoformat()
        (projected_root / "sessions.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        index = HistoryIndex(projected_root)
        with engine.begin() as db:
            db.exec_driver_sql("DELETE FROM cursors")
            index._sessions(db)
            projected = [
                json.loads(row[0]) for row in db.exec_driver_sql("SELECT record_json FROM sessions")
            ]
            chats = [
                json.loads(row[0])
                for row in db.exec_driver_sql("SELECT record_json FROM session_chats")
            ]
        fields = ("chat_id", "session_instance_id", "harness", "native_store", "harness_session_id")
        assert Counter(tuple(row.get(f) for f in fields) for row in projected) == (
            Counter(tuple(row.model_dump().get(f) for f in fields) for row in expected)
        )
        accepted = golden.s.list_all_session_records(root)
        assert {r["chat_id"]: tuple(r.get(f) for f in fields[2:]) for r in chats} == {
            r.chat_id: tuple(r.model_dump().get(f) for f in fields[2:]) for r in accepted
        }
        return expected

    monkeypatch.setattr(golden.s, "list_session_generations", check_projection)
    golden.test_generated_histories_match_pre_restructure_fold(tmp_path)


def test_metadata_is_history_blind(tmp_path: Path):
    from meridian.lib.state import spawn_store

    key = str(
        spawn_store.start_spawn(
            tmp_path, chat_id="c1", model="test", agent="test", harness="codex", prompt="test"
        )
    )
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    _append(tmp_path, _start("g1", spawn_id=key))
    history = tmp_path / "spawns" / key / "history.jsonl"
    # Even a malformed or unfinished tail must not affect metadata discovery.
    history.write_text('{"timestamp":"2099-01-01T00:00:00Z"}\ninvalid tail')
    index = HistoryIndex(tmp_path)
    index.rebuild()
    present = _rows(index)
    history.unlink()
    index.rebuild()
    assert _rows(index) == present
