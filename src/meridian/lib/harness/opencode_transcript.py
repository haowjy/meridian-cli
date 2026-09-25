"""OpenCode transcript provider with opencode.db preference."""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from itertools import groupby
from pathlib import Path
from typing import Literal, Protocol, cast

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.opencode_storage import resolve_opencode_home_dir
from meridian.lib.state.native_search_index import OpenCodeV1Witness, OpenCodeV2Witness
from meridian.lib.state.native_snapshot import TranscriptValidation

OpenCodeDbSchema = Literal["sqlite_v1", "sqlite_v2"]

_V2_RECORD = "opencode.transcript.v2"
_V2_VERSION = 2


def resolve_opencode_db_path(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve the OpenCode SQLite database path.

    Precedence: ``OPENCODE_DB`` (absolute, or relative to the OpenCode data
    dir; ``:memory:`` is preserved verbatim) → ``OPENCODE_HOME``/``opencode.db``
    → ``$XDG_DATA_HOME/opencode/opencode.db`` →
    ``~/.local/share/opencode/opencode.db``.
    """

    env = launch_env if launch_env is not None else os.environ
    override = env.get("OPENCODE_DB", "").strip()
    if override:
        if override == ":memory:":
            return Path(":memory:")
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate
        return resolve_opencode_home_dir(launch_env) / candidate
    return resolve_opencode_home_dir(launch_env) / "opencode.db"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    # SQLite may update WAL shared-memory read marks in mode=ro. This is the
    # same normal read behavior used by live OpenCode session-log reads.
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def detect_opencode_db_schema(db_path: Path | None = None) -> OpenCodeDbSchema | None:
    """Detect an OpenCode database's schema family from its tables.

    V2 is identified by the presence of ``session_v2``; V1 by ``session``. Returns
    ``None`` when the database is absent or carries neither table. The installed
    binary is never consulted.
    """

    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return None
    with closing(_connect_readonly(resolved_db_path)) as connection:
        names = _table_names(connection)
    if "session_v2" in names:
        return "sqlite_v2"
    if "session" in names:
        return "sqlite_v1"
    return None


def opencode_db_session_exists(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> bool:
    """Return whether opencode.db contains a session row for ``session_id``."""

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return False
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return False

    with closing(
        sqlite3.connect(resolved_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        row = connection.execute(
            "SELECT 1 FROM session WHERE id = ?", (normalized_session_id,)
        ).fetchone()
    return row is not None


def opencode_db_v2_session_exists(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> bool:
    """Return whether a V2 ``session_v2`` table contains ``session_id``.

    A missing database or ``session_v2`` table is not existence evidence; it is
    reported as ``False`` rather than raising.
    """

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return False
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return False

    try:
        with closing(_connect_readonly(resolved_db_path)) as connection:
            if "session_v2" not in _table_names(connection):
                return False
            row = connection.execute(
                "SELECT 1 FROM session_v2 WHERE id = ?", (normalized_session_id,)
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def opencode_db_any_session_exists(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> bool:
    """Return whether either OpenCode schema family contains ``session_id``.

    V2 is checked when ``session_v2`` is present, V1 when only ``session`` is.
    A migrated database keeps both tables but V2 copies every session, so the V2
    probe is authoritative there. A database with neither table is not existence
    evidence; a corrupt database still surfaces its read error.
    """

    if not session_id.strip():
        return False
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return False
    with closing(_connect_readonly(resolved_db_path)) as connection:
        names = _table_names(connection)
        table = "session_v2" if "session_v2" in names else "session" if "session" in names else None
        if table is None:
            return False
        # Exact identity checks must distinguish unreadable authority from absent IDs.
        # In particular, a torn import snapshot must defer, never persist "missing".
        return (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (session_id.strip(),)
            ).fetchone()
            is not None
        )


class _JsonlEventReader(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        current: Callable[[], bool] | None = None,
        validation: TranscriptValidation | None = None,
    ) -> Iterator[dict[str, object]]: ...


class OpenCodeStorageTranscriptProvider:
    """OpenCode storage provider that prefers opencode.db transcript rows."""

    def __init__(
        self,
        *,
        iter_json_events: _JsonlEventReader,
    ) -> None:
        self._iter_json_events = iter_json_events

    def supports(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name in {"session_diff", "session"}
            and path.parent.parent.name == "storage"
        )

    def iter_events(
        self,
        path: Path,
        *,
        current: Callable[[], bool] | None = None,
        validation: TranscriptValidation | None = None,
    ) -> Iterator[dict[str, object]]:
        database = opencode_db_for_session_file(path)
        if database is not None and opencode_db_session_exists(
            session_id=path.stem, db_path=database
        ):
            yield from iter_opencode_db_events(session_id=path.stem, db_path=database)
            return
        yield from self._iter_json_events(path, current=current, validation=validation)


class OpenCodeV2StorageTranscriptProvider:
    """OpenCode V2 storage provider reading ``session_v2``/``session_message``.

    Sibling of :class:`OpenCodeStorageTranscriptProvider`. Selection is by schema
    presence (``session_v2``), never by the installed binary. A missing table or
    fresh database yields no events instead of raising.
    """

    def __init__(
        self,
        *,
        iter_json_events: _JsonlEventReader,
    ) -> None:
        self._iter_json_events = iter_json_events

    def supports(self, path: Path) -> bool:
        database = opencode_db_for_session_file(path)
        return database is not None and detect_opencode_db_schema(database) == "sqlite_v2"

    def iter_events(
        self,
        path: Path,
        *,
        current: Callable[[], bool] | None = None,
        validation: TranscriptValidation | None = None,
    ) -> Iterator[dict[str, object]]:
        database = opencode_db_for_session_file(path)
        if database is not None:
            yield from iter_opencode_v2_db_events(session_id=path.stem, db_path=database)
            return
        yield from self._iter_json_events(path, current=current, validation=validation)


def opencode_db_for_session_file(path: Path) -> Path | None:
    if path.parent.name not in {"session_diff", "session"}:
        return None
    storage_root = path.parent.parent
    return storage_root.parent / "opencode.db" if storage_root.name == "storage" else None


def _load_json_object(value: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed)


def _text_from_mapping(mapping: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_body(state: dict[str, object]) -> str:
    raw_input = state.get("input")
    if not isinstance(raw_input, dict):
        return ""
    tool_input = cast("dict[str, object]", raw_input)
    direct = _text_from_mapping(
        tool_input,
        (
            "command",
            "filePath",
            "file_path",
            "path",
            "pattern",
            "description",
        ),
    )
    if direct:
        return direct
    if not tool_input:
        return ""
    try:
        return json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(tool_input)


def _tool_output(state: dict[str, object]) -> str:
    output = state.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    metadata = state.get("metadata")
    if isinstance(metadata, dict):
        metadata_output = cast("dict[str, object]", metadata).get("output")
        if isinstance(metadata_output, str) and metadata_output.strip():
            return metadata_output.strip()
    return ""


def _tool_events(part: dict[str, object]) -> list[dict[str, object]]:
    state = part.get("state")
    if not isinstance(state, dict):
        return []
    state_payload = cast("dict[str, object]", state)
    if str(state_payload.get("status", "")).strip().lower() != "completed":
        return []

    tool_name = str(part.get("tool", "tool")).strip() or "tool"
    body = _tool_body(state_payload)
    events: list[dict[str, object]] = [
        {
            "event_type": "response_item",
            "type": "function_call",
            "name": tool_name,
            "arguments": body,
        }
    ]
    output = _tool_output(state_payload)
    if output:
        events.append(
            {
                "event_type": "response_item",
                "type": "function_call_output",
                "output": output,
            }
        )
    return events


def _message_events(
    *,
    role: str,
    parts: list[dict[str, object]],
) -> tuple[list[dict[str, object]], str | None]:
    events: list[dict[str, object]] = []
    reason: str | None = None
    malformed = "Malformed OpenCode part content; rendering is incomplete."
    for part in parts:
        kind = part.get("type")
        if not isinstance(kind, str):
            reason = malformed
            continue
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                reason = malformed
            elif text.strip():
                events.append({"role": role, "content": text.strip()})
        elif kind == "tool":
            state = part.get("state")
            name = part.get("tool")
            if not isinstance(state, dict) or not isinstance(name, str) or not name.strip():
                reason = malformed
                continue
            state = cast("dict[str, object]", state)
            if state.get("status") != "completed":
                reason = "Unrendered OpenCode tool state; rendering is incomplete."
                continue
            if not isinstance(state.get("input"), dict) or not isinstance(state.get("output"), str):
                reason = malformed
            events.extend(_tool_events(part))
        elif kind == "reasoning":
            if not isinstance(part.get("text"), str):
                reason = malformed
        elif kind not in {"step-start", "step-finish", "snapshot", "patch", "compaction"}:
            reason = "Unsupported OpenCode part content; rendering is incomplete."
    return events, reason


def _is_compaction_message(message_payload: dict[str, object]) -> bool:
    role = str(message_payload.get("role", "")).strip().lower()
    mode = str(message_payload.get("mode", "")).strip().lower()
    agent = str(message_payload.get("agent", "")).strip().lower()
    return role == "assistant" and (
        message_payload.get("summary") is True or (mode == "compaction" and agent == "compaction")
    )


def _compaction_handoff_event(
    *,
    message_payload: dict[str, object],
    parts: list[dict[str, object]],
) -> dict[str, object]:
    event: dict[str, object] = {
        "role": "assistant",
        "mode": "compaction",
        "agent": "compaction",
    }
    if parts:
        event["parts"] = parts
    else:
        raw_parts = message_payload.get("parts")
        if isinstance(raw_parts, list):
            event["parts"] = raw_parts
        raw_part = message_payload.get("part")
        if isinstance(raw_part, dict):
            event["part"] = raw_part
    content = message_payload.get("content")
    if content is not None:
        event["content"] = content
    return event


def _raw_row(row: sqlite3.Row) -> dict[str, object]:
    """Preserve the three native tables' SQLite values in the raw-row dialect."""
    result: dict[str, object] = {}
    for key, value in zip(row.keys(), row, strict=True):
        if isinstance(value, bytes):
            result[key] = {"sqlite_type": "blob", "base64": base64.b64encode(value).decode("ascii")}
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Unsupported non-finite OpenCode column value")
        elif value is None or isinstance(value, (str, int, float)):
            result[key] = value
        else:
            raise ValueError("Unsupported OpenCode column value")
    return result


def iter_opencode_db_events(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> Generator[dict[str, object]]:
    """Read the complete raw MessageV2 transcript scope in one read-only transaction.

    A session row is positive existence evidence, including valid-empty sessions.
    Each message carries its own raw part rows; unmatched session parts follow.
    No role, material type, payload string or native column is discarded here.
    This consistent read alone does not qualify unfinished responses for capture.
    """
    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        raise ValueError("OpenCode transcript requires an exact session ID")
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        raise FileNotFoundError(resolved_db_path)
    with closing(
        sqlite3.connect(resolved_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        yield from _iter_v1_events(connection, normalized_session_id)


def _iter_v1_events(
    connection: sqlite3.Connection, normalized_session_id: str
) -> Generator[dict[str, object]]:
    for table, required in (
        ("session", {"id", "time_created", "time_updated"}),
        ("message", {"id", "session_id", "time_created", "time_updated", "data"}),
        ("part", {"id", "session_id", "message_id", "time_created", "time_updated", "data"}),
    ):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise ValueError(f"Unsupported OpenCode {table} schema")
    session = connection.execute(
        "SELECT * FROM session WHERE id=?", (normalized_session_id,)
    ).fetchone()
    if session is None:
        raise ValueError("OpenCode transcript session does not exist")
    yield {
        "record": "opencode.transcript",
        "version": 1,
        "table": "session",
        "row": _raw_row(session),
    }
    # Traverse selected parts once. A per-message session+message predicate
    # can pick the native session-only index and rescan the session N times.
    parts = connection.execute(
        "SELECT p.* FROM part p CROSS JOIN message m "
        "WHERE p.session_id=? AND m.id=p.message_id AND m.session_id=p.session_id "
        "ORDER BY m.time_created,m.id,p.time_created,p.id",
        (normalized_session_id,),
    )
    groups = groupby(parts, key=lambda part: part["message_id"])
    pending = next(groups, None)
    for message in connection.execute(
        "SELECT * FROM message WHERE session_id=? ORDER BY time_created,id",
        (normalized_session_id,),
    ):
        message_parts: list[dict[str, object]] = []
        if pending is not None and pending[0] == message["id"]:
            message_parts = [_raw_row(part) for part in pending[1]]
            pending = next(groups, None)
        yield {
            "record": "opencode.transcript",
            "version": 1,
            "table": "message",
            "row": _raw_row(message),
            "parts": message_parts,
        }
    for part in connection.execute(
        "SELECT * FROM part p WHERE p.session_id=? AND NOT EXISTS "
        "(SELECT 1 FROM message m WHERE m.id=p.message_id AND m.session_id=p.session_id) "
        "ORDER BY p.time_created,p.id",
        (normalized_session_id,),
    ):
        yield {
            "record": "opencode.transcript",
            "version": 1,
            "table": "part",
            "row": _raw_row(part),
        }


def iter_opencode_v2_db_events(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> Generator[dict[str, object]]:
    """Read V2 ``session_v2``/``session_message`` rows in the V2 raw dialect.

    The session header carries the authoritative ``session_v2`` row (id, model,
    parent_id, idle_outcome, title, ...). Each ``session_message`` row follows in
    ``seq`` order with its JSON payload preserved. A missing database, missing
    table, or unknown session yields no events instead of raising: a fresh V2
    database is a valid empty observation and must not look like corrupt data.
    """

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return

    with closing(_connect_readonly(resolved_db_path)) as connection:
        connection.row_factory = sqlite3.Row
        names = _table_names(connection)
        if "session_v2" not in names or "session_message" not in names:
            return
        connection.execute("BEGIN")
        yield from _iter_v2_events(connection, normalized_session_id)


def _iter_v2_events(
    connection: sqlite3.Connection, normalized_session_id: str
) -> Generator[dict[str, object]]:
    session = connection.execute(
        "SELECT * FROM session_v2 WHERE id=?", (normalized_session_id,)
    ).fetchone()
    if session is None:
        return
    yield {
        "record": _V2_RECORD,
        "version": _V2_VERSION,
        "type": "session",
        "data": _raw_row(session),
    }
    for message in connection.execute(
        "SELECT type,seq,data FROM session_message WHERE session_id=? ORDER BY seq,time_created,id",
        (normalized_session_id,),
    ):
        payload = _load_json_object(message["data"])
        yield {
            "record": _V2_RECORD,
            "version": _V2_VERSION,
            "type": str(message["type"]),
            "seq": message["seq"],
            "session_id": normalized_session_id,
            "data": payload if payload is not None else {},
        }


def iter_opencode_db_session_events(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> Generator[dict[str, object]]:
    """Dispatch to the V2 reader when ``session_v2`` is present, else V1.

    Schema is decided by table presence. When neither table is readable the V1
    reader runs, preserving its existing raise-on-unavailable behavior for
    callers that already establish positive session identity.
    """

    if detect_opencode_db_schema(db_path) == "sqlite_v2":
        yield from iter_opencode_v2_db_events(session_id=session_id, db_path=db_path)
        return
    yield from iter_opencode_db_events(session_id=session_id, db_path=db_path)


OpenCodeWitness = OpenCodeV1Witness | OpenCodeV2Witness


def _session_witnesses(
    connection: sqlite3.Connection, session_ids: Iterable[str]
) -> dict[str, OpenCodeWitness]:
    ids = json.dumps(list(session_ids))
    if "session_v2" in _table_names(connection):
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


def _model_ref_text(raw_model: object) -> str | None:
    if not isinstance(raw_model, str) or not raw_model.strip():
        return None
    parsed = _load_json_object(raw_model)
    if parsed is None:
        return None
    provider = parsed.get("providerID") or parsed.get("provider")
    model_id = parsed.get("id") or parsed.get("modelID")
    if not isinstance(provider, str) or not provider.strip():
        return None
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return f"{provider.strip()}/{model_id.strip()}"


def read_last_model(
    session_id: str,
    *,
    db_path: Path | None = None,
    launch_env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the session's last used model as ``provider/model``.

    Reads ``session_v2.model`` first; falls back to ``session.model`` only when
    ``session_v2`` is absent. Returns ``None`` for a missing session, absent
    database, or unparseable model reference.
    """

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return None
    resolved_db_path = db_path or resolve_opencode_db_path(launch_env)
    if not resolved_db_path.is_file():
        return None

    raw_model: object = None
    try:
        with closing(_connect_readonly(resolved_db_path)) as connection:
            names = _table_names(connection)
            if "session_v2" in names:
                row = connection.execute(
                    "SELECT model FROM session_v2 WHERE id=?", (normalized_session_id,)
                ).fetchone()
                if row is not None:
                    raw_model = row[0]
            if raw_model is None and "session" in names:
                row = connection.execute(
                    "SELECT model FROM session WHERE id=?", (normalized_session_id,)
                ).fetchone()
                if row is not None:
                    raw_model = row[0]
    except sqlite3.Error:
        return None
    return _model_ref_text(raw_model)


def interpret_opencode_record(
    event: dict[str, object],
    *,
    include_user_setup: bool,
) -> tuple[list[dict[str, object]], bool, str | None]:
    """Translate a preserved row group once, at the shared normalization boundary.

    Return display events, whether this is a user message, and any rendering limit.
    Unknown/malformed material stays in raw authority, never successful empty display.
    """
    reason = "Malformed OpenCode transcript rows; rendering is incomplete."
    if event.get("version") != 1:
        return [], False, "Unsupported OpenCode transcript dialect; rendering is incomplete."
    row = event.get("row")
    if not isinstance(row, dict):
        return [], False, reason
    row = cast("dict[str, object]", row)
    table = event.get("table")
    if table == "session":
        return [], False, None
    if table != "message":
        return [], False, "Unassociated OpenCode transcript part; rendering is incomplete."
    message = _load_json_object(row.get("data"))
    raw_parts = event.get("parts")
    if message is None or not isinstance(raw_parts, list):
        return [], False, reason
    role = str(message.get("role", "")).strip().lower()
    if role not in {"assistant", "user", "system"}:
        return [], False, "Unsupported OpenCode message role; rendering is incomplete."
    parts: list[dict[str, object]] = []
    rendering_reason: str | None = None
    for raw_part in cast("list[object]", raw_parts):
        part = (
            _load_json_object(cast("dict[str, object]", raw_part).get("data"))
            if isinstance(raw_part, dict)
            else None
        )
        if part is None:
            rendering_reason = reason
            continue
        parts.append(part)
    material, material_reason = _message_events(role=role, parts=parts)
    rendering_reason = rendering_reason or material_reason
    if _is_compaction_message(message):
        return (
            [
                {"part": {"type": "compaction"}},
                _compaction_handoff_event(message_payload=message, parts=parts),
            ],
            False,
            rendering_reason,
        )
    events: list[dict[str, object]] = []
    if role == "user" and include_user_setup:
        system = _text_from_value(message.get("system"))
        if system:
            events.append({"opencode_db_setup": system})
    events.extend(material)
    return events, role == "user", rendering_reason


def _v2_tool_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    texts: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, dict):
            continue
        item_payload = cast("dict[str, object]", item)
        if str(item_payload.get("type", "")).strip().lower() != "text":
            continue
        text = item_payload.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n".join(texts)


def _v2_tool_part(part: dict[str, object]) -> dict[str, object]:
    raw_state = part.get("state")
    state = dict(cast("dict[str, object]", raw_state)) if isinstance(raw_state, dict) else {}
    name = part.get("name") or part.get("tool")
    normalized: dict[str, object] = {
        "type": "tool",
        "tool": name if isinstance(name, str) and name.strip() else "tool",
        "state": state,
    }
    if not isinstance(state.get("output"), str):
        output = _v2_tool_text(state.get("content"))
        if output:
            state["output"] = output
    return normalized


def _v2_content_parts(content: object) -> list[dict[str, object]]:
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for item in cast("list[object]", content):
        if not isinstance(item, dict):
            continue
        part = cast("dict[str, object]", item)
        parts.append(
            _v2_tool_part(part) if str(part.get("type", "")).strip().lower() == "tool" else part
        )
    return parts


def interpret_opencode_v2_record(
    event: dict[str, object],
    *,
    include_user_setup: bool,
) -> tuple[list[dict[str, object]], bool, str | None]:
    """Translate one preserved V2 row into display events at the shared boundary.

    ``session_message.type`` is the message role: ``user`` carries top-level
    ``text``; ``assistant`` carries a ``content`` part list. Unknown or malformed
    rows yield no display and a rendering limit, never a successful empty.
    """

    _ = include_user_setup
    reason = "Malformed OpenCode V2 transcript row; rendering is incomplete."
    if event.get("version") != _V2_VERSION:
        return [], False, "Unsupported OpenCode V2 transcript dialect; rendering is incomplete."
    message_type = str(event.get("type", "")).strip().lower()
    if message_type in {"session", "idle", "model-switched"}:
        return [], False, None
    data = event.get("data")
    if not isinstance(data, dict):
        return [], False, reason
    data_payload = cast("dict[str, object]", data)
    if message_type == "user":
        text = _text_from_value(data_payload.get("text"))
        return ([{"role": "user", "content": text}] if text else []), True, None
    if message_type == "assistant":
        parts = _v2_content_parts(data_payload.get("content"))
        material, material_reason = _message_events(role="assistant", parts=parts)
        return list(material), False, material_reason
    return [], False, "Unsupported OpenCode V2 message type; rendering is incomplete."


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def extract_last_assistant_report(events: Iterable[dict[str, object]]) -> str | None:
    """Read response text incrementally through the same raw-row interpretation."""
    from meridian.lib.harness.transcript import DefaultTranscriptEventParser, TranscriptNormalizer

    normalizer, parser = TranscriptNormalizer(), DefaultTranscriptEventParser()
    last_assistant: str | None = None
    for event in events:
        for message in normalizer.feed(event, parser).messages:
            if (
                message.role == "assistant"
                and message.tool_call is None
                and not message.is_tool_result
            ):
                last_assistant = message.content
    return last_assistant


def extract_last_assistant_report_from_session_path(path: Path) -> str | None:
    """Return the last assistant message text for one OpenCode session file."""

    def _no_json(
        path: Path,
        *,
        current: Callable[[], bool] | None = None,
        validation: TranscriptValidation | None = None,
    ) -> Iterator[dict[str, object]]:
        del path, current, validation
        return iter(())

    provider = OpenCodeStorageTranscriptProvider(iter_json_events=_no_json)
    return extract_last_assistant_report(provider.iter_events(path))


__all__ = [
    "OpenCodeDbSchema",
    "OpenCodeStorageTranscriptProvider",
    "OpenCodeV2StorageTranscriptProvider",
    "detect_opencode_db_schema",
    "extract_last_assistant_report",
    "extract_last_assistant_report_from_session_path",
    "interpret_opencode_v2_record",
    "iter_opencode_db_events",
    "iter_opencode_db_session_events",
    "iter_opencode_v2_db_events",
    "opencode_db_any_session_exists",
    "opencode_db_session_exists",
    "opencode_db_v2_session_exists",
    "read_last_model",
    "resolve_opencode_db_path",
]


def read_opencode_v2_turn(key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
    """Only event-named V2 replies in the recorded session/store can supply facts."""
    if not turn_ids or not Path(key.native_store).is_file():
        return None
    with closing(_connect_readonly(Path(key.native_store))) as connection:
        connection.row_factory = sqlite3.Row
        if "session_message" not in _table_names(connection):
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
