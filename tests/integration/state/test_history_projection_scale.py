"""Guard the sessions projection against per-line database round-trips.

Cold history-index initialization runs under a 60s budget. Rebuilding a real
corpus regressed to ~19s when every ``sessions.jsonl`` event was projected with
its own ORM SELECT/INSERT: ~115k statements for ~24k log lines. The projection
keeps its working set in memory and publishes in bulk, so a multi-thousand line
log must execute fewer statements than it has lines. Per-line behavior issues
roughly one statement per event per table and fails this guard by a wide margin.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine

from meridian.lib.platform.locking import lock_file
from meridian.lib.state import spawn_store
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.session_fold import SessionRecord
from meridian.lib.state.session_store import SessionStartEvent, SessionUpdateEvent


def _write_sessions_log(root: Path, *, spawns: int = 6, events_per_spawn: int = 200) -> int:
    lines: list[str] = []
    for i in range(spawns):
        key = spawn_store.start_spawn(
            root, chat_id=f"c{i + 1}", prompt="p", model="m", agent="coder", harness="codex"
        ).spawn_id
        spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
        record = spawn_store.get_spawn(root, key)
        assert record is not None
        chat_id, history_id = record.chat_id, record.history_id
        generation = f"gen-{i}"
        lines.append(
            SessionStartEvent(
                chat_id=chat_id,
                kind="spawn",
                harness="codex",
                harness_session_id=f"hs-{i}",
                model="m",
                session_instance_id=generation,
                started_at="2026-09-19T00:00:00+00:00",
                history_id=history_id,
                spawn_id=key,
            ).model_dump_json()
        )
        for _ in range(events_per_spawn):
            lines.append(
                SessionUpdateEvent(
                    chat_id=chat_id,
                    harness_session_id=f"hs-{i}",
                    session_instance_id=generation,
                    active_work_id=f"w{i}",
                    spawn_id=key,
                ).model_dump_json()
            )
    # Generation-less sessions exercise the legacy fallback and its alias path.
    for i in range(spawns):
        chat_id = f"L{i + 1}"
        lines.append(
            SessionStartEvent(
                chat_id=chat_id,
                kind="primary",
                harness="codex",
                harness_session_id=f"hl-{i}",
                model="m",
                started_at="2026-09-19T00:00:00+00:00",
            ).model_dump_json()
        )
        for _ in range(events_per_spawn):
            lines.append(
                SessionUpdateEvent(
                    chat_id=chat_id,
                    harness_session_id=f"hl-{i}",
                    active_work_id=f"lw{i}",
                ).model_dump_json()
            )
    (root / "sessions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def test_sessions_projection_avoids_per_line_round_trips(tmp_path: Path) -> None:
    lines = _write_sessions_log(tmp_path)
    statements = 0
    index = HistoryIndex(tmp_path)

    @event.listens_for(Engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany) -> None:
        nonlocal statements
        statements += 1

    try:
        index.rebuild(reset=True, timeout=60)
    finally:
        event.remove(Engine, "before_cursor_execute", _count)

    # A statement bound is only meaningful beside the work it bounds: a
    # disabled projection would also issue no statements.
    assert {record.chat_id for record in index.sessions()} == {f"c{i + 1}" for i in range(6)} | {
        f"L{i + 1}" for i in range(6)
    }
    assert index.work_chat_ids("w0") == {"c1"}
    assert statements < lines, (
        f"Sessions projection issued {statements} statements for {lines} lines; "
        "per-line round-trips are back"
    )


def test_incremental_sessions_projection_validates_only_the_touched_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_sessions_log(tmp_path, spawns=10, events_per_spawn=1)
    index = HistoryIndex(tmp_path)
    index.rebuild(reset=True)
    changes = HistoryChanges(tmp_path)
    source = HistorySource(kind="sessions")
    with (
        lock_file(changes.mutation_lock, mode="shared"),
        lock_file(source.lock_path(tmp_path)),
    ):
        changes.mark(source)
        with (tmp_path / "sessions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                SessionUpdateEvent(
                    chat_id="c1",
                    harness_session_id="hs-0",
                    session_instance_id="gen-0",
                    active_work_id="changed",
                ).model_dump_json()
                + "\n"
            )

    validations = 0
    validate_json = SessionRecord.model_validate_json

    def count_validation(cls, json_data, **kwargs):
        nonlocal validations
        validations += 1
        return validate_json(json_data, **kwargs)

    monkeypatch.setattr(SessionRecord, "model_validate_json", classmethod(count_validation))

    index.catch_up()

    # One accepted-chat row plus its one generation. Counting decoded records
    # protects the expensive work without coupling the test to SQL spelling.
    assert validations == 2
