"""Crash-recoverable SQLite projection over authoritative session JSONL state."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import Any, cast

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.paths import RuntimePaths

type SessionPayload = dict[str, Any]
type SessionReducer = Callable[
    [SessionPayload | None, SessionPayload],
    SessionPayload | None,
]
@dataclass(frozen=True)
class SessionBackfillResult:
    payloads: list[SessionPayload]
    generation_before: str
    generation_after: str


@dataclass(frozen=True)
class SessionBackfill:
    generation: Callable[[Path], str]
    enrich: Callable[[Path, list[SessionPayload]], SessionBackfillResult]

_SCHEMA_VERSION = 3
_SOURCE_META_KEYS = (
    "source_dev",
    "source_ino",
    "source_offset",
    "source_mtime_ns",
    "source_ctime_ns",
)
_SOURCE_CHECKPOINT_KEY = "source_checkpoint_v1"
_SPAWN_BACKFILL_KEY = "primary_spawn_backfill_generation_v1"
_CHECKPOINT_BYTES = 4096
_SQLITE_TIMEOUT_SECONDS = 0.05


class SessionIndexUnavailable(OSError):
    """The derived index could not be read or rebuilt."""


def _chat_order(chat_id: str) -> int:
    if chat_id.startswith("c") and chat_id[1:].isdigit():
        return int(chat_id[1:])
    return 2**63 - 1


def _source_state(path: Path) -> tuple[int, int, int, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (0, 0, 0, 0, 0)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        os.chmod(path, 0o600)
    except OSError:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    version = cast("int", connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, _SCHEMA_VERSION}:
        raise sqlite3.DatabaseError(f"unsupported session index schema {version}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id TEXT PRIMARY KEY,
            chat_order INTEGER NOT NULL,
            kind TEXT NOT NULL,
            activity_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS sessions_recent
        ON sessions(kind, activity_at DESC, chat_order DESC)
        """
    )
    if version == 0:
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('primary_count', '0')"
        )
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    connection.commit()


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        cast("str", row["key"]): cast("str", row["value"])
        for row in connection.execute("SELECT key, value FROM metadata")
    }


def _write_source_metadata(
    connection: sqlite3.Connection,
    source_path: Path,
    source_state: tuple[int, int, int, int, int],
    *,
    offset: int,
) -> None:
    dev, ino, _size, mtime_ns, ctime_ns = source_state
    values = {
        "source_dev": str(dev),
        "source_ino": str(ino),
        "source_offset": str(offset),
        "source_mtime_ns": str(mtime_ns),
        "source_ctime_ns": str(ctime_ns),
        _SOURCE_CHECKPOINT_KEY: _source_checkpoint(source_path, offset),
    }
    connection.executemany(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        values.items(),
    )


def _source_checkpoint(source_path: Path, offset: int) -> str:
    """Fingerprint the indexed tail so truncate/rewrite cannot masquerade as append."""

    digest = blake2b(digest_size=16)
    if offset <= 0 or not source_path.is_file():
        return digest.hexdigest()
    start = max(0, offset - _CHECKPOINT_BYTES)
    with source_path.open("rb") as handle:
        handle.seek(start)
        digest.update(handle.read(offset - start))
    return digest.hexdigest()


def _upsert_payload(connection: sqlite3.Connection, payload: SessionPayload) -> None:
    chat_id = payload.get("chat_id")
    kind = payload.get("kind")
    started_at = payload.get("started_at")
    stopped_at = payload.get("stopped_at")
    if not isinstance(chat_id, str) or not isinstance(kind, str):
        raise sqlite3.DatabaseError("session projection is missing identity fields")
    if not isinstance(started_at, str):
        raise sqlite3.DatabaseError("session projection is missing started_at")
    activity_at = stopped_at if isinstance(stopped_at, str) and stopped_at else started_at
    connection.execute(
        """
        INSERT OR REPLACE INTO sessions(
            chat_id, chat_order, kind, activity_at, record_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            _chat_order(chat_id),
            kind,
            activity_at,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        ),
    )


def _apply_event(
    connection: sqlite3.Connection,
    event_payload: SessionPayload,
    reducer: SessionReducer,
) -> None:
    chat_id = event_payload.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        return
    chat_id = chat_id.strip()
    row = connection.execute(
        "SELECT record_json FROM sessions WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    current: SessionPayload | None = None
    if row is not None:
        decoded = json.loads(cast("str", row["record_json"]))
        if not isinstance(decoded, dict):
            raise sqlite3.DatabaseError("invalid session record projection")
        current = cast("SessionPayload", decoded)
    updated = reducer(current, event_payload)
    if updated is not None:
        was_primary = current is not None and current.get("kind") == "primary"
        is_primary = updated.get("kind") == "primary"
        if was_primary != is_primary:
            connection.execute(
                """
                UPDATE metadata
                SET value = CAST(value AS INTEGER) + ?
                WHERE key = 'primary_count'
                """,
                (1 if is_primary else -1,),
            )
        if is_primary and not updated.get("spawn_id"):
            connection.execute(
                "DELETE FROM metadata WHERE key = ?",
                (_SPAWN_BACKFILL_KEY,),
            )
        _upsert_payload(connection, updated)


def _ingest_complete_lines(
    connection: sqlite3.Connection,
    source_path: Path,
    *,
    start_offset: int,
    reducer: SessionReducer,
) -> int:
    if not source_path.is_file():
        return 0
    offset = start_offset
    with source_path.open("rb") as handle:
        handle.seek(start_offset)
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            offset = handle.tell()
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                _apply_event(connection, cast("SessionPayload", payload), reducer)
    return offset


def _rebuild(
    connection: sqlite3.Connection,
    source_path: Path,
    source_state: tuple[int, int, int, int, int],
    reducer: SessionReducer,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM metadata")
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('primary_count', '0')"
        )
        offset = _ingest_complete_lines(
            connection,
            source_path,
            start_offset=0,
            reducer=reducer,
        )
        _write_source_metadata(connection, source_path, source_state, offset=offset)
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def _sync(
    connection: sqlite3.Connection,
    source_path: Path,
    reducer: SessionReducer,
) -> None:
    source_state = _source_state(source_path)
    metadata = _metadata(connection)
    try:
        recorded = tuple(int(metadata[key]) for key in _SOURCE_META_KEYS)
    except (KeyError, ValueError):
        _rebuild(connection, source_path, source_state, reducer)
        return

    dev, ino, size, mtime_ns, ctime_ns = source_state
    recorded_dev, recorded_ino, offset, recorded_mtime_ns, recorded_ctime_ns = recorded
    recorded_checkpoint = metadata.get(_SOURCE_CHECKPOINT_KEY)
    if (
        (dev, ino) != (recorded_dev, recorded_ino)
        or size < offset
        or (
            size == offset
            and (mtime_ns, ctime_ns) != (recorded_mtime_ns, recorded_ctime_ns)
        )
        or (
            size > offset
            and recorded_checkpoint != _source_checkpoint(source_path, offset)
        )
    ):
        _rebuild(connection, source_path, source_state, reducer)
        return
    if size == offset:
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        updated_offset = _ingest_complete_lines(
            connection,
            source_path,
            start_offset=offset,
            reducer=reducer,
        )
        _write_source_metadata(
            connection,
            source_path,
            source_state,
            offset=updated_offset,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def _ensure_spawn_backfill(
    connection: sqlite3.Connection,
    runtime_root: Path,
    backfill: SessionBackfill | None,
) -> None:
    if backfill is None:
        return
    generation = backfill.generation(runtime_root)
    completed_generation = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (_SPAWN_BACKFILL_KEY,),
    ).fetchone()
    if (
        completed_generation is not None
        and cast("str", completed_generation["value"]) == generation
    ):
        return
    rows = connection.execute(
        "SELECT record_json FROM sessions WHERE kind = 'primary'"
    ).fetchall()
    payloads = _decode_rows(rows)
    result = backfill.enrich(runtime_root, payloads)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for payload in result.payloads:
            _upsert_payload(connection, payload)
        if result.generation_before == result.generation_after:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (_SPAWN_BACKFILL_KEY, result.generation_after),
            )
        else:
            connection.execute(
                "DELETE FROM metadata WHERE key = ?",
                (_SPAWN_BACKFILL_KEY,),
            )
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def _reset(path: Path) -> None:
    for candidate in (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm")):
        with suppress(OSError):
            candidate.unlink(missing_ok=True)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    return getattr(exc, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    } or any(token in str(exc).lower() for token in ("busy", "locked"))


def _read(
    runtime_root: Path,
    reducer: SessionReducer,
    query: Callable[[sqlite3.Connection], Any],
    *,
    backfill: SessionBackfill | None = None,
) -> Any:
    paths = RuntimePaths.from_root_dir(runtime_root)
    if not paths.sessions_jsonl.is_file() and not paths.session_index_db.is_file():
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            _initialize_schema(connection)
            _ensure_spawn_backfill(connection, runtime_root, backfill)
            return query(connection)

    with lock_file(paths.sessions_flock):
        for attempt in range(2):
            try:
                with _connect(paths.session_index_db) as connection:
                    _initialize_schema(connection)
                    _sync(connection, paths.sessions_jsonl, reducer)
                    _ensure_spawn_backfill(connection, runtime_root, backfill)
                    return query(connection)
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc):
                    raise SessionIndexUnavailable(str(exc)) from exc
                _reset(paths.session_index_db)
                if attempt:
                    raise SessionIndexUnavailable(str(exc)) from exc
            except (
                json.JSONDecodeError,
                OSError,
                sqlite3.DatabaseError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                _reset(paths.session_index_db)
                if attempt:
                    raise SessionIndexUnavailable(str(exc)) from exc
    raise AssertionError("unreachable")


def _decode_rows(rows: Collection[sqlite3.Row]) -> list[SessionPayload]:
    payloads: list[SessionPayload] = []
    for row in rows:
        decoded = json.loads(cast("str", row["record_json"]))
        if not isinstance(decoded, dict):
            raise sqlite3.DatabaseError("invalid session record projection")
        payloads.append(cast("SessionPayload", decoded))
    return payloads


def get_session_payload(
    runtime_root: Path,
    chat_id: str,
    reducer: SessionReducer,
    backfill: SessionBackfill | None = None,
) -> SessionPayload | None:
    def query(connection: sqlite3.Connection) -> SessionPayload | None:
        row = connection.execute(
            "SELECT record_json FROM sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return _decode_rows((row,))[0] if row is not None else None

    return cast(
        "SessionPayload | None",
        _read(runtime_root, reducer, query, backfill=backfill),
    )


def get_session_payloads(
    runtime_root: Path,
    chat_ids: Collection[str],
    reducer: SessionReducer,
    backfill: SessionBackfill | None = None,
) -> list[SessionPayload]:
    normalized = tuple(sorted({chat_id.strip() for chat_id in chat_ids if chat_id.strip()}))
    if not normalized:
        return []

    def query(connection: sqlite3.Connection) -> list[SessionPayload]:
        connection.execute("CREATE TEMP TABLE wanted_sessions(chat_id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO wanted_sessions(chat_id) VALUES (?)",
            ((chat_id,) for chat_id in normalized),
        )
        rows = connection.execute(
            """
            SELECT sessions.record_json
            FROM sessions JOIN wanted_sessions USING(chat_id)
            """
        ).fetchall()
        return _decode_rows(rows)

    return cast(
        "list[SessionPayload]",
        _read(runtime_root, reducer, query, backfill=backfill),
    )


def list_session_payloads(
    runtime_root: Path,
    reducer: SessionReducer,
    backfill: SessionBackfill | None = None,
) -> list[SessionPayload]:
    def query(connection: sqlite3.Connection) -> list[SessionPayload]:
        rows = connection.execute(
            "SELECT record_json FROM sessions ORDER BY chat_order, chat_id"
        ).fetchall()
        return _decode_rows(rows)

    return cast(
        "list[SessionPayload]",
        _read(runtime_root, reducer, query, backfill=backfill),
    )


def list_recent_primary_payloads(
    runtime_root: Path,
    *,
    limit: int,
    live_chat_ids: Collection[str],
    reducer: SessionReducer,
    backfill: SessionBackfill | None = None,
) -> tuple[list[SessionPayload], int]:
    if limit <= 0:
        raise ValueError("limit must be positive")

    def query(connection: sqlite3.Connection) -> tuple[list[SessionPayload], int]:
        connection.execute("CREATE TEMP TABLE live_sessions(chat_id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT OR IGNORE INTO live_sessions(chat_id) VALUES (?)",
            ((chat_id,) for chat_id in live_chat_ids),
        )
        total_row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'primary_count'"
        ).fetchone()
        if total_row is None:
            raise sqlite3.DatabaseError("session index is missing primary_count")
        total = int(cast("str", total_row["value"]))
        rows = connection.execute(
            """
            SELECT sessions.record_json
            FROM sessions JOIN live_sessions USING(chat_id)
            WHERE sessions.kind = 'primary'
            ORDER BY activity_at DESC, chat_order DESC, chat_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        remaining = limit - len(rows)
        if remaining:
            rows.extend(
                connection.execute(
                    """
                    SELECT record_json
                    FROM sessions
                    WHERE kind = 'primary'
                      AND NOT EXISTS(
                          SELECT 1 FROM live_sessions
                          WHERE live_sessions.chat_id = sessions.chat_id
                      )
                    ORDER BY activity_at DESC, chat_order DESC, chat_id DESC
                    LIMIT ?
                    """,
                    (remaining,),
                ).fetchall()
            )
        return _decode_rows(rows), total

    return cast(
        "tuple[list[SessionPayload], int]",
        _read(runtime_root, reducer, query, backfill=backfill),
    )


def primary_spawn_backfill_is_current(
    runtime_root: Path,
    reducer: SessionReducer,
    backfill: SessionBackfill,
) -> bool:
    """Return whether spawn discovery covers the current published generation."""

    def query(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (_SPAWN_BACKFILL_KEY,),
        ).fetchone()
        return row is not None and cast("str", row["value"]) == backfill.generation(
            runtime_root
        )

    return cast("bool", _read(runtime_root, reducer, query, backfill=backfill))


__all__ = [
    "SessionBackfill",
    "SessionBackfillResult",
    "SessionIndexUnavailable",
    "SessionPayload",
    "SessionReducer",
    "get_session_payload",
    "get_session_payloads",
    "list_recent_primary_payloads",
    "list_session_payloads",
    "primary_spawn_backfill_is_current",
]
