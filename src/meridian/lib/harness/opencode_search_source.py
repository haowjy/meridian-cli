"""Read-only OpenCode snapshots and freshness witnesses for native readers."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import sqlite3
from collections.abc import Generator, Iterable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.native_witness import OpenCodeV1Witness, OpenCodeV2Witness
from meridian.lib.harness.opencode_transcript import (
    _V2_RECORD,
    _V2_VERSION,
    _connect_readonly,
    _iter_v1_events,
    _iter_v2_events,
    _load_json_object,
    _schema,
    extract_last_assistant_report,
)

OpenCodeWitness = OpenCodeV1Witness | OpenCodeV2Witness


def _session_witnesses(
    connection: sqlite3.Connection, session_ids: Iterable[str]
) -> dict[str, OpenCodeWitness]:
    ids = json.dumps(list(session_ids))
    if _schema(connection) == "sqlite_v2":
        rows = connection.execute(
            "WITH wanted AS (SELECT value AS id FROM json_each(?)), "
            "messages AS (SELECT session_id,count(*) AS n,max(seq) AS seq,"
            "max(time_updated) AS updated FROM session_message "
            "WHERE session_id IN (SELECT id FROM wanted) GROUP BY session_id) "
            "SELECT s.id,coalesce(m.n,0),m.seq,m.updated,s.time_updated "
            "FROM session_v2 s JOIN wanted w ON s.id=w.id "
            "LEFT JOIN messages m ON m.session_id=s.id",
            (ids,),
        )
        return {str(r[0]): OpenCodeV2Witness(*r[1:]) for r in rows}
    rows = connection.execute(
        "WITH wanted AS (SELECT value AS id FROM json_each(?)), "
        "parts AS (SELECT session_id,count(*) AS n,max(time_updated) AS updated "
        "FROM part WHERE session_id IN (SELECT id FROM wanted) GROUP BY session_id), "
        "messages AS (SELECT session_id,count(*) AS n,max(time_updated) AS updated "
        "FROM message WHERE session_id IN (SELECT id FROM wanted) GROUP BY session_id) "
        "SELECT s.id,coalesce(p.n,0),p.updated,coalesce(m.n,0),m.updated,s.time_updated "
        "FROM session s JOIN wanted w ON s.id=w.id "
        "LEFT JOIN parts p ON p.session_id=s.id LEFT JOIN messages m ON m.session_id=s.id",
        (ids,),
    )
    return {str(r[0]): OpenCodeV1Witness(*r[1:]) for r in rows}


def opencode_session_witnesses(
    db_path: Path, session_ids: Iterable[str]
) -> dict[str, OpenCodeWitness]:
    """Grouped existence and freshness check in the recorded DB, never ambient storage."""
    with closing(_connect_readonly(db_path)) as connection:
        connection.execute("BEGIN")
        return _session_witnesses(connection, session_ids)


@contextmanager
def read_opencode_search_source(
    db_path: Path, session_id: str
) -> Generator[tuple[OpenCodeWitness, Iterator[dict[str, object]]]]:
    """Witness and raw native events share one short read-only snapshot."""
    with closing(_connect_readonly(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        witness = _session_witnesses(connection, (session_id,)).get(session_id)
        if witness is None:
            raise ValueError("OpenCode transcript session does not exist")
        reader = _iter_v2_events if isinstance(witness, OpenCodeV2Witness) else _iter_v1_events
        yield witness, reader(connection, session_id)


def read_opencode_v2_turn(key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
    """Only event-named V2 replies in the recorded session/store can supply facts."""
    if not turn_ids or not Path(key.native_store).is_file():
        return None
    with closing(_connect_readonly(Path(key.native_store))) as connection:
        connection.row_factory = sqlite3.Row
        if _schema(connection) != "sqlite_v2":
            return None
        for message_id in reversed(turn_ids):
            row = connection.execute(
                "SELECT type,seq,data FROM session_message WHERE session_id=? AND id=?",
                (key.session_id, message_id),
            ).fetchone()
            if row is not None:
                report = extract_last_assistant_report(
                    [
                        {
                            "record": _V2_RECORD,
                            "version": _V2_VERSION,
                            "type": row["type"],
                            "seq": row["seq"],
                            "session_id": key.session_id,
                            "data": _load_json_object(row["data"]) or {},
                        }
                    ]
                )
                if report:
                    return report
    return None
