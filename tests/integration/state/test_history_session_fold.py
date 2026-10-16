"""The disposable projection must replay the same accepted bindings as authority."""

import hashlib
import json
import random
import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store as s
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.session_store import list_session_generations

# Generated replay golden, recorded from ae9a7c2a before the binding rewrite.
#
# The digest covers complete serialized generation and chat records, not a subset
# of fields. No real journal data is retained. Regenerate the reference only from
# the baseline, never from the implementation under test.


def _run_golden(tmp_path: Path) -> None:
    results = []
    for seed in range(1000):
        rng = random.Random(seed)
        events = []
        for i in range(35):
            event = rng.choice(["start", "start", "update", "update", "stop"])
            row = {
                "event": event,
                "chat_id": rng.choice(["c1", "c2"]),
                "session_instance_id": rng.choice(["", "a", "b", " a "]),
            }
            if event == "start":
                row.update(
                    harness=rng.choice(["claude", "codex"]),
                    harness_session_id=rng.choice(["", "id1", "id2", None]),
                    native_store=rng.choice([None, "", "/a", "/b"]),
                    model="test",
                    started_at=str(i),
                )
            elif event == "update":
                row.update(
                    harness_session_id=rng.choice(["", "id1", "id2", None]),
                    native_store=rng.choice([None, "", "/a", "/b"]),
                    active_work_id="work",
                    source=rng.choice(["legacy_import", "observed"]),
                )
            events.append(row)
        # An inert historical chat then a new generation; store-only and attempt links.
        historical = s.SessionRecord(
            chat_id="c3",
            kind="spawn",
            harness="codex",
            harness_session_id="historical",
            model="test",
            agent="",
            agent_path="",
            skills=(),
            skill_paths=(),
            params=(),
            started_at="0",
            stopped_at="1",
            session_instance_id="historical-old",
            record_mode="historical",
        )
        events.extend(
            [
                {"event": "historical_import", "record": historical.model_dump(mode="json")},
                {
                    "event": "start",
                    "chat_id": "c3",
                    "harness": "codex",
                    "harness_session_id": "historical",
                    "model": "test",
                    "started_at": "2",
                    "session_instance_id": "new",
                },
                {
                    "event": "update",
                    "chat_id": "c3",
                    "session_instance_id": "new",
                    "native_store": "/import",
                    "source": "legacy_import",
                },
                {
                    "event": "update",
                    "chat_id": "c3",
                    "session_instance_id": "new",
                    "harness_session_id": "historical",
                    "startup_attempt_id": "retry",
                },
            ]
        )
        (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        with capture_logs() as logs:
            result = [
                # Compare the pre-restructure schema, not later additive settings.
                [r.model_dump(mode="json", exclude={"launch_policy_snapshot"}) for r in f(tmp_path)]
                for f in (s.list_session_generations, s.list_all_session_records)
            ]
        assert not logs
        results.append(result)
    serialized = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == (
        "f5c5954b1f3a2ecf2754d99cfe1e6733984c7e4128fc9c8c6fef017f79f9cc32"
    )


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


def test_incremental_replay_waits_for_partial_line_then_resumes(tmp_path: Path) -> None:
    _append(
        tmp_path,
        _start("g1", chat_id="c1", harness_session_id="native-1"),
        _start("g2", chat_id="c2", harness_session_id="native-2"),
    )
    index = HistoryIndex(tmp_path)
    index.rebuild()
    sessions_path = tmp_path / "sessions.jsonl"
    assert sorted(record.chat_id for record in index.sessions()) == ["c1", "c2"]

    uncommitted = _start(
        "g3",
        chat_id="c3",
        harness_session_id="native-3",
    )
    changes = HistoryChanges(tmp_path)
    source = HistorySource(kind="sessions")
    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(source.lock_path(tmp_path)),
    ):
        changes.mark(source)
        with sessions_path.open("ab") as handle:
            handle.write(json.dumps(uncommitted).encode("utf-8"))

    assert sorted(record.chat_id for record in index.sessions()) == ["c1", "c2"]
    with sqlite3.connect(index.path) as db:
        cursor_before_completion = db.execute(
            "SELECT extent FROM cursors WHERE source = 'sessions'"
        ).fetchone()
    assert cursor_before_completion == (
        sessions_path.stat().st_size - len(json.dumps(uncommitted).encode("utf-8")),
    )

    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(source.lock_path(tmp_path)),
    ):
        changes.mark(source)
        with sessions_path.open("ab") as handle:
            handle.write(b"\n")

    assert sorted(record.chat_id for record in index.sessions()) == ["c1", "c2", "c3"]

    final_event = _start(
        "g5",
        chat_id="c5",
        harness_session_id="native-5",
    )
    with sessions_path.open("ab") as handle:
        handle.write(b"{malformed middle row}\n")
        handle.write((json.dumps(final_event) + "\n").encode("utf-8"))
    HistoryChanges(tmp_path).mark(HistorySource(kind="sessions"))

    assert sorted(record.chat_id for record in index.sessions()) == [
        "c1",
        "c2",
        "c3",
        "c5",
    ]
    with sqlite3.connect(index.path) as db:
        cursor = db.execute(
            "SELECT extent FROM cursors WHERE source = 'sessions'"
        ).fetchone()
    assert cursor == (sessions_path.stat().st_size,)


def test_generated_histories_index_bindings_match_authority(tmp_path: Path, monkeypatch):
    """Run P5's unchanged 1,000-seed generator and golden, checking the index too."""
    from collections import Counter
    from datetime import UTC, datetime

    from sqlalchemy import create_engine

    from meridian.lib.state.history_index import INDEX_SCHEMA

    original = s.list_session_generations
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
        accepted = s.list_all_session_records(root)
        assert {r["chat_id"]: tuple(r.get(f) for f in fields[2:]) for r in chats} == {
            r.chat_id: tuple(r.model_dump().get(f) for f in fields[2:]) for r in accepted
        }
        return expected

    monkeypatch.setattr(s, "list_session_generations", check_projection)
    _run_golden(tmp_path)


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
