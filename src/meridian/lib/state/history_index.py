"""One disposable metadata projection, shared by history discovery surfaces.

Writers only invalidate sources. This module alone projects authoritative files
and acknowledges changes. Lifecycle decisions must still read the stores.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, NamedTuple, NoReturn
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    tuple_,
    union_all,
)
from sqlalchemy.engine import Connection, Engine  # noqa: TC002
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import ColumnElement  # noqa: TC002

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.history_changes import (
    SCHEMA_VERSION,
    DirtySource,
    HistoryChanges,
    HistoryCoordinationError,
    HistorySource,
    versioned,
)
from meridian.lib.state.history_codec import canonical_time, last_activity
from meridian.lib.state.jsonl import iter_jsonl_file
from meridian.lib.state.session_fold import (
    SessionHistoricalEvent,
    SessionRecord,
    SessionStartEvent,
    SessionUpdateEvent,
    UnsupportedSessionRecords,
    parse_event,
    project_session_generation,
    supported_session_event_version,
)
from meridian.lib.state.session_store import parse_model_observation
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
    read_state,
    scan_spawn_ids,
)
from meridian.lib.state.spawn_store import SpawnScan

logger = logging.getLogger(__name__)

INITIALIZATION_TIMEOUT = 15.0
QUERY_TIMEOUT = 2.0
_REBUILD_COMMAND = "meridian session index rebuild --metadata-only"

INDEX_SCHEMA = MetaData()
META = Table(
    "meta",
    INDEX_SCHEMA,
    Column("version", Integer, nullable=False),
    Column("generation", Text, nullable=False),
    Column("build", Text, nullable=False),
)
RECORDS = Table(
    "records",
    INDEX_SCHEMA,
    Column("history_id", Text, primary_key=True),
    Column("local_id", Text),
    Column("chat", Text),
    Column("owner", Text),
    Column("parent", Text),
    Column("work", Text),
    Column("status", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("started", Text, nullable=False),
    Column("activity", Text, nullable=False),
    Column("active", Integer, nullable=False),
    Column("archive_id", Text),
    Column("record_json", Text, nullable=False),
)
Index("loose_alias", RECORDS.c.local_id, unique=True, sqlite_where=RECORDS.c.archive_id.is_(None))
Index("owner_records", RECORDS.c.owner, RECORDS.c.started)
Index("chat_records", RECORDS.c.chat, RECORDS.c.started)
Index("parent_records", RECORDS.c.parent)
Index("work_records", RECORDS.c.work, RECORDS.c.started)
Index("status_records", RECORDS.c.status, RECORDS.c.started)
Index("activity_records", RECORDS.c.activity)
Index("active_records", RECORDS.c.local_id, sqlite_where=RECORDS.c.active == 1)
LOCATIONS = Table(
    "locations",
    INDEX_SCHEMA,
    Column("source_id", Text, primary_key=True),
    Column("history_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("activity", Text, nullable=False),
    Column("record_json", Text, nullable=False),
    Column("receipt_json", Text),
    Column("portable_digest", Text),
)
Index(
    "history_locations",
    LOCATIONS.c.history_id,
    LOCATIONS.c.kind,
    LOCATIONS.c.ordinal.desc(),
)
ARCHIVE_HEADS = Table(
    "archive_heads",
    INDEX_SCHEMA,
    Column("history_id", Text, primary_key=True),
    Column("portable_digest", Text, nullable=False),
)
ALIASES = Table(
    "aliases",
    INDEX_SCHEMA,
    Column("source_id", Text, nullable=False, primary_key=True),
    Column("alias", Text, nullable=False, primary_key=True),
    Column("kind", Text, nullable=False, primary_key=True),
    Column("history_id", Text, nullable=False, primary_key=True),
)
Index("history_aliases", ALIASES.c.alias, ALIASES.c.kind, ALIASES.c.history_id)
SESSIONS = Table(
    "sessions",
    INDEX_SCHEMA,
    Column("chat", Text, nullable=False, primary_key=True),
    Column("generation", Text, nullable=False, primary_key=True),
    Column("ordinal", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("stopped", Text),
    Column("activity", Text, nullable=False),
    Column("history_id", Text),
    Column("record_json", Text, nullable=False),
)
Index("session_recency", SESSIONS.c.kind, SESSIONS.c.activity.desc())
Index("session_history", SESSIONS.c.history_id, SESSIONS.c.activity.desc())
# The accepted chat can differ from its generation bucket (raw vs normalized
# generation spelling). Keep it independently, with the blank-bucket pointer.
SESSION_CHATS = Table(
    "session_chats",
    INDEX_SCHEMA,
    Column("chat", Text, primary_key=True),
    Column("ordinal", Integer, nullable=False),
    Column("latest_blank", Text),
    Column("record_json", Text, nullable=False),
)
MODEL_OBSERVATIONS = Table(
    "model_observations",
    INDEX_SCHEMA,
    Column("harness", Text, primary_key=True),
    Column("session", Text, primary_key=True),
    Column("model", Text, nullable=False),
)
LINEAGE = Table(
    "session_lineage",
    INDEX_SCHEMA,
    Column("child", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("parent", Text, nullable=False),
    Column("history_id", Text),
)
Index("lineage_parent", LINEAGE.c.parent)
WORK_CHATS = Table(
    "work_chats",
    INDEX_SCHEMA,
    Column("work", Text, nullable=False, primary_key=True),
    Column("chat", Text, nullable=False, primary_key=True),
)
CURSORS = Table(
    "cursors",
    INDEX_SCHEMA,
    Column("source", Text, primary_key=True),
    Column("inode", Text),
    Column("extent", Integer),
    Column("tail", Text),
    # Sorted JSON list of unsupported session versions already passed by the cursor.
    Column("unsupported", Text),
)


def _metadata_match(
    chat: ColumnElement[str],
    record_json: ColumnElement[str],
    session_json: ColumnElement[str],
    needle: str,
) -> ColumnElement[bool]:
    """A browse row's displayed metadata contains ``needle`` (lowercase).

    ``session_json`` is the session record that owns the tmux target and native identity:
    the record itself for a session, the archive receipt's session for an archive (NULL
    when the archive has no receipt).
    """

    def field(json: ColumnElement[str], *paths: str) -> ColumnElement[str]:
        return func.coalesce(*(func.json_extract(json, path) for path in paths), "")

    haystack = func.lower(
        func.coalesce(chat, "")
        .concat(" ")
        .concat(field(session_json, "$.tmux_target"))
        .concat(" ")
        .concat(field(record_json, "$.active_work_id", "$.work_id"))
        .concat(" ")
        .concat(field(record_json, "$.agent"))
        .concat(" ")
        .concat(field(record_json, "$.model"))
        .concat(" ")
        .concat(field(record_json, "$.task_cwd", "$.execution_cwd"))
    )
    # An archive without a receipt shows the model observed for its own record's identity.
    identity = func.coalesce(session_json, record_json)
    observed = exists().where(
        MODEL_OBSERVATIONS.c.harness == field(identity, "$.harness"),
        MODEL_OBSERVATIONS.c.session == field(identity, "$.harness_session_id"),
        func.instr(func.lower(MODEL_OBSERVATIONS.c.model), needle) > 0,
    )
    return or_(func.instr(haystack, needle) > 0, observed)


class BrowsePage(NamedTuple):
    """One browse query: records in order, the matching total, and archive sessions.

    ``archived`` maps an archive record's history id to the session its receipt retained.
    """

    records: list[SessionRecord | SpawnRecord]
    total: int
    archived: dict[str, SessionRecord]


class HistoryIndexIncomplete(RuntimeError):
    """Discovery cannot safely promise complete candidate membership."""


class HistoryIndexBusy(HistoryIndexIncomplete):
    """Transient contention prevented a complete history-index read."""


@dataclass(frozen=True)
class IndexCoverage:
    generation: str
    build: str
    complete: bool
    pending: tuple[str, ...] = ()
    activity_provisional: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexStatus:
    baseline: Literal["absent", "current", "incompatible", "corrupt", "failed"]
    schema: int | None = None
    generation: str | None = None
    build: str | None = None
    reason: str | None = None


class _InitializationFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal[1] = 1
    target_schema: int
    generation: str | None
    code: Literal["timeout", "io", "sqlite", "authority"]
    reason: str = Field(max_length=1024)
    failed_at: str


class HistorySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    history_id: str
    archive_id: str
    portable_digest: str
    current: bool
    path: str


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("History index deadline exhausted")
    return remaining


def _source_label(source: HistorySource) -> str:
    return f"spawn:{source.key}" if source.kind == "spawn" else source.kind


@contextmanager
def _source_lock(path: Path, deadline: float) -> Generator[None]:
    """Past the deadline, one try-lock still tells contention from exhaustion."""
    remaining = deadline - time.monotonic()
    with lock_file(path, timeout=max(remaining, 0.0)):
        if remaining <= 0:
            raise TimeoutError("History index deadline exhausted")
        yield


def _reraise_dbapi(exc: DBAPIError) -> NoReturn:
    if exc.orig is not None:
        raise exc.orig from exc
    raise exc


def _engine(
    path: Path,
    *,
    fresh: bool = False,
    timeout: float = 2,
    readonly: bool = False,
    autocommit: bool = False,
) -> Engine:
    def creator() -> sqlite3.Connection:
        if readonly:
            return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
        return sqlite3.connect(path, timeout=timeout)

    engine = (
        create_engine(
            "sqlite+pysqlite://",
            creator=creator,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        if autocommit
        else create_engine("sqlite+pysqlite://", creator=creator, poolclass=NullPool)
    )
    if not readonly:

        def configure(dbapi_connection: sqlite3.Connection, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=FULL")
                if not fresh:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        event.listen(engine, "connect", configure)

    return engine


@contextmanager
def _connect(
    path: Path,
    *,
    fresh: bool = False,
    timeout: float = 2,
    readonly: bool = False,
    autocommit: bool = False,
) -> Generator[Connection]:
    engine = _engine(path, fresh=fresh, timeout=timeout, readonly=readonly, autocommit=autocommit)
    try:
        try:
            with engine.connect() as db:
                try:
                    yield db
                except DBAPIError as exc:
                    _reraise_dbapi(exc)
        except DBAPIError as exc:
            _reraise_dbapi(exc)
    finally:
        engine.dispose()


def _tail(handle: BinaryIO, extent: int, count: int = 256) -> str:
    position = handle.tell()
    try:
        handle.seek(max(0, extent - count))
        return hashlib.sha256(handle.read(min(extent, count))).hexdigest()
    finally:
        handle.seek(position)


@dataclass(frozen=True)
class HistoryIndex:
    """One format's projection. Every owned path names both input and index versions.

    A schema bump starts a new file built fresh from authority. Older builds keep
    their own file (0.6.7 and earlier: ``history.sqlite3``), which this build never
    opens, so there is no in-place migration.
    """

    root: Path

    @property
    def directory(self) -> Path:
        return self.root / "history-index"

    @property
    def path(self) -> Path:
        return self.directory / f"{versioned('history')}.sqlite3"

    @property
    def stage(self) -> Path:
        # catchup_lock owns this disposable stage, including crash residue.
        return self.directory / f".{versioned('build')}.sqlite3"

    @property
    def catchup_lock(self) -> Path:
        return self.root / "locks" / f"{versioned('history-catchup')}.lock"

    @property
    def database_lock(self) -> Path:
        return self.root / "locks" / f"{versioned('history-database')}.lock"

    def _aliases(
        self,
        db: Connection,
        source_id: str,
        history_id: str,
        state: SpawnRecord | None = None,
        session: SessionRecord | None = None,
    ) -> None:
        db.exec_driver_sql("DELETE FROM aliases WHERE source_id = ?", (source_id,))
        rows = self._alias_rows(source_id, history_id, state=state, session=session)
        if rows:
            db.exec_driver_sql(
                "INSERT OR IGNORE INTO aliases (source_id, alias, kind, history_id) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    @staticmethod
    def _alias_rows(
        source_id: str,
        history_id: str,
        state: SpawnRecord | None = None,
        session: SessionRecord | None = None,
    ) -> list[tuple[str, str, str, str]]:
        names: set[tuple[str, str]] = set()
        if state:
            names.add((state.id, "spawn"))
            if state.chat_id and (
                state.kind == "primary"
                or (state.owner_chat_id is not None and state.owner_chat_id != state.chat_id)
            ):
                names.add((state.chat_id, "chat"))
            if state.harness_session_id:
                names.add((state.harness_session_id, "harness"))
        if session:
            names.add((session.chat_id, "chat"))
            if session.harness_session_id:
                names.add((session.harness_session_id, "harness"))
        return [(source_id, name, kind, history_id) for name, kind in names]

    def _refresh(self, db: Connection, history_id: str) -> None:
        location = (
            db.exec_driver_sql(
                "SELECT kind, activity, record_json, receipt_json FROM locations "
                "WHERE history_id = ? AND (kind = 'spawn' OR portable_digest = "
                "(SELECT portable_digest FROM archive_heads "
                "WHERE archive_heads.history_id = locations.history_id)) "
                "ORDER BY (kind = 'spawn') DESC, ordinal DESC, source_id LIMIT 1",
                (history_id,),
            )
            .mappings()
            .first()
        )
        db.exec_driver_sql("DELETE FROM records WHERE history_id = ?", (history_id,))
        if location is None:
            return
        record = SpawnRecord.model_validate_json(location["record_json"])
        receipt = json.loads(location["receipt_json"]) if location["receipt_json"] else None
        active = record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES
        activity = (
            location["activity"]
            if location["kind"] == "spawn"
            else last_activity(record, None, location["activity"])
        )
        db.exec_driver_sql(
            "INSERT INTO records (history_id, local_id, chat, owner, parent, work, status, "
            "kind, started, activity, active, archive_id, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                history_id,
                record.id,
                record.chat_id,
                record.owner_chat_id or record.chat_id,
                record.parent_id,
                record.work_id,
                str(record.status),
                record.kind,
                canonical_time(record.started_at or ""),
                activity,
                int(active and receipt is None),
                receipt["archive_id"] if receipt else None,
                record.model_dump_json(),
            ),
        )

    def _spawn(self, db: Connection, key: str) -> bool:
        source_id = f"spawn:{key}"
        old = db.exec_driver_sql(
            "SELECT history_id, record_json FROM locations WHERE source_id = ?", (source_id,)
        ).first()
        record = read_state(self.root / "spawns", key, include_prompt=False)
        active = record is not None and (
            record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES
        )
        if record is not None and old and old[1] == record.model_dump_json():
            return active  # Unchanged: rewriting would only cost a FULL-sync commit.
        db.exec_driver_sql("DELETE FROM locations WHERE source_id = ?", (source_id,))
        db.exec_driver_sql("DELETE FROM aliases WHERE source_id = ?", (source_id,))
        if old:
            self._refresh(db, old[0])
        if record is None:
            return False
        history_id = str(record.history_id or uuid5(NAMESPACE_URL, f"{self.root}:{key}"))
        if db.exec_driver_sql(
            "SELECT 1 FROM locations WHERE history_id = ? AND kind = 'spawn'", (history_id,)
        ).first():
            raise ValueError(f"Conflicting loose copies of history {history_id}")
        activity = canonical_time(
            record.terminal.finished_at if record.terminal else record.started_at or "",
        )
        db.exec_driver_sql(
            "INSERT INTO locations (source_id, history_id, kind, ordinal, activity, "
            "record_json, receipt_json, portable_digest) "
            "VALUES (?, ?, 'spawn', 0, ?, ?, NULL, NULL)",
            (source_id, history_id, activity, record.model_dump_json()),
        )
        self._aliases(db, source_id, history_id, state=record)
        self._refresh(db, history_id)
        return active

    def _sessions(self, db: Connection) -> None:
        path = self.root / "sessions.jsonl"
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            db.execute(delete(SESSIONS))
            db.execute(delete(SESSION_CHATS))
            db.execute(delete(WORK_CHATS))
            db.execute(delete(MODEL_OBSERVATIONS))
            db.execute(delete(LINEAGE))
            db.execute(delete(ALIASES).where(ALIASES.c.source_id.like("session:%")))
            db.execute(delete(CURSORS).where(CURSORS.c.source == "sessions"))
            return
        with handle:
            stat = os.fstat(handle.fileno())
            snapshot_extent = stat.st_size
            inode = f"{stat.st_dev}:{stat.st_ino}"
            cursor = (
                db.execute(select(CURSORS).where(CURSORS.c.source == "sessions")).mappings().first()
            )
            offset = 0
            unsupported = UnsupportedSessionRecords()
            if (
                cursor
                and cursor["inode"] == inode
                and cursor["extent"] <= snapshot_extent
                and cursor["tail"] == _tail(handle, cursor["extent"])
            ):
                if cursor["extent"] == snapshot_extent:
                    return  # Nothing appended since the cursor.
                offset = cursor["extent"]
                unsupported.versions.update(json.loads(cursor["unsupported"] or "[]"))
            else:
                db.execute(delete(SESSIONS))
                db.execute(delete(SESSION_CHATS))
                db.execute(delete(WORK_CHATS))
                db.execute(delete(MODEL_OBSERVATIONS))
                db.execute(delete(LINEAGE))
                db.execute(delete(ALIASES).where(ALIASES.c.source_id.like("session:%")))
            # Load a chat's prior state only when an appended event touches it.
            # Metadata is derived once per changed generation, not mirrored per event.
            records: dict[str, SessionRecord] = {}
            latest_blank: dict[str, str] = {}
            chat_ordinals: dict[str, int] = {}
            generations: dict[tuple[str, str], dict[str, SessionRecord]] = {}
            open_generations: dict[str, set[str]] = {}
            ordinals: dict[tuple[str, str], int] = {}
            loaded_chats: set[str] = set()
            dirty: set[tuple[str, str]] = set()
            dirty_chats: set[str] = set()
            work_chats: set[tuple[str, str]] = set()

            def parse_index_event(payload: dict[str, object]):
                if not supported_session_event_version(payload, unsupported):
                    return None
                observation = parse_model_observation(payload)
                if observation is not None:
                    db.execute(
                        insert(MODEL_OBSERVATIONS)
                        .prefix_with("OR REPLACE")
                        .values(
                            harness=observation.harness,
                            session=observation.harness_session_id,
                            model=observation.observed_model_token,
                        )
                    )
                    return None
                return parse_event(payload, unsupported)

            for parsed_event, end in iter_jsonl_file(
                handle,
                parse_index_event,
                path=path,
                offset=offset,
                extent=snapshot_extent,
            ):
                if isinstance(parsed_event, SessionUpdateEvent) and parsed_event.active_work_id:
                    work_chats.add((parsed_event.active_work_id.strip(), parsed_event.chat_id))
                if parsed_event is not None:
                    chat = parsed_event.chat_id
                    if chat not in loaded_chats:
                        accepted = db.exec_driver_sql(
                            "SELECT ordinal, latest_blank, record_json FROM session_chats "
                            "WHERE chat = ?",
                            (chat,),
                        ).first()
                        if accepted is not None:
                            chat_ordinals[chat] = accepted[0]
                            if accepted[1] is not None:
                                latest_blank[chat] = accepted[1]
                            records[chat] = SessionRecord.model_validate_json(accepted[2])
                        for generation, ordinal, raw in db.exec_driver_sql(
                            "SELECT generation, ordinal, record_json FROM sessions WHERE chat = ?",
                            (chat,),
                        ):
                            generation_record = SessionRecord.model_validate_json(raw)
                            generations[(chat, generation)] = {chat: generation_record}
                            if (
                                generation_record.record_mode == "live"
                                and generation_record.stopped_at is None
                            ):
                                open_generations.setdefault(chat, set()).add(generation)
                            ordinals[(chat, generation)] = ordinal
                        loaded_chats.add(chat)
                    prior = records.get(chat)
                    changed = project_session_generation(
                        records,
                        generations,
                        open_generations,
                        latest_blank,
                        parsed_event,
                        ordinal=offset,
                    )
                    started = isinstance(parsed_event, (SessionStartEvent, SessionHistoricalEvent))
                    if records.get(chat) is not prior:
                        dirty_chats.add(chat)
                        if started:
                            chat_ordinals[chat] = offset
                    dirty.update(changed)
                    if started and changed:
                        started_generation = parsed_event.session_instance_id
                        if isinstance(parsed_event, SessionStartEvent) and not started_generation:
                            started_generation = latest_blank[chat]
                        started_key = (chat, started_generation)
                        if started_key in changed:
                            ordinals[started_key] = offset
                offset = end
            for chat, generation in dirty:
                record = generations[(chat, generation)][chat]
                history_id = str(record.history_id) if record.history_id else None
                if history_id is None and record.spawn_id:
                    linked = db.exec_driver_sql(
                        "SELECT history_id FROM locations WHERE source_id = ?",
                        (f"spawn:{record.spawn_id}",),
                    ).first()
                    history_id = linked[0] if linked else None
                if history_id is None and generation:
                    linked = db.exec_driver_sql(
                        "SELECT history_id FROM records WHERE chat = ? AND archive_id IS NULL "
                        "AND json_extract(record_json, '$.session_instance_id') = ? LIMIT 1",
                        (chat, generation),
                    ).first()
                    history_id = linked[0] if linked else None
                db.exec_driver_sql(
                    "INSERT OR REPLACE INTO sessions (chat, generation, ordinal, kind, stopped, "
                    "activity, history_id, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chat,
                        generation,
                        ordinals[(chat, generation)],
                        record.kind,
                        record.stopped_at,
                        canonical_time(record.stopped_at or record.started_at),
                        history_id,
                        record.model_dump_json(),
                    ),
                )
                source_id = f"session:{chat}:{generation}"
                if history_id:
                    self._aliases(db, source_id, history_id, session=record)
                else:
                    db.exec_driver_sql("DELETE FROM aliases WHERE source_id = ?", (source_id,))
            for chat in dirty_chats:
                record = records[chat]
                db.execute(delete(LINEAGE).where(LINEAGE.c.child == chat))
                if record.kind == "primary":
                    for kind in ("forked", "context"):
                        parent = getattr(record, f"{kind}_from_chat_id")
                        history = getattr(record, f"{kind}_from_history_id")
                        if parent:
                            db.execute(
                                insert(LINEAGE).values(
                                    child=chat,
                                    kind=kind,
                                    parent=parent,
                                    history_id=str(history) if history else None,
                                )
                            )
            if dirty_chats:
                db.exec_driver_sql(
                    "INSERT OR REPLACE INTO session_chats "
                    "(chat, ordinal, latest_blank, record_json) VALUES (?, ?, ?, ?)",
                    [
                        (
                            chat,
                            chat_ordinals[chat],
                            latest_blank.get(chat),
                            records[chat].model_dump_json(),
                        )
                        for chat in dirty_chats
                    ],
                )
            if work_chats:
                db.exec_driver_sql(
                    "INSERT OR IGNORE INTO work_chats (work, chat) VALUES (?, ?)",
                    list(work_chats),
                )
            db.exec_driver_sql(
                "INSERT OR REPLACE INTO cursors (source, inode, extent, tail, unsupported) "
                "VALUES ('sessions', ?, ?, ?, ?)",
                (
                    inode,
                    offset,
                    _tail(handle, offset),
                    json.dumps(sorted(unsupported.versions)) if unsupported.versions else None,
                ),
            )

    def _warn_unsupported_sessions(self, db: Connection) -> None:
        """Every indexed read repeats the warning for rows the cursor skipped earlier."""
        stored = db.execute(
            select(CURSORS.c.unsupported).where(CURSORS.c.source == "sessions")
        ).scalar()
        if stored:
            UnsupportedSessionRecords(set(json.loads(stored))).warn(self.root / "sessions.jsonl")

    def _project(self, db: Connection, source: HistorySource) -> bool:
        if source.kind == "spawn":
            return self._spawn(db, source.key)
        if source.kind == "sessions":
            self._sessions(db)
            return False
        self._catalog(db)
        return False

    def _catalog(self, db: Connection) -> None:
        from meridian.lib.state.retention_archive import catalog_heads, read_receipts

        receipts = read_receipts(self.root)
        db.execute(delete(ARCHIVE_HEADS))
        heads = [
            {"history_id": history_id, "portable_digest": digest}
            for history_id, digest in catalog_heads(receipts).items()
        ]
        if heads:
            db.execute(insert(ARCHIVE_HEADS), heads)
        affected = {
            row[0]
            for row in db.execute(
                select(LOCATIONS.c.history_id).where(LOCATIONS.c.kind == "archive")
            )
        }
        db.execute(delete(LOCATIONS).where(LOCATIONS.c.kind == "archive"))
        db.execute(delete(ALIASES).where(ALIASES.c.source_id.like("archive:%")))
        for ordinal, receipt in enumerate(receipts):
            for record in receipt.records:
                history_id = str(record.history_id)
                source_id = f"archive:{receipt.archive_id}:{receipt.location_id}:{history_id}"
                db.execute(
                    insert(LOCATIONS)
                    .prefix_with("OR REPLACE")
                    .values(
                        source_id=source_id,
                        history_id=history_id,
                        kind="archive",
                        ordinal=ordinal,
                        activity=canonical_time(record.activity),
                        record_json=record.state.model_dump_json(),
                        receipt_json=receipt.model_copy(
                            update={"records": (record,)}
                        ).model_dump_json(),
                        portable_digest=record.portable_digest,
                    )
                )
                self._aliases(db, source_id, history_id, state=record.state, session=record.session)
                affected.add(history_id)
        for history_id in affected:
            self._refresh(db, history_id)

    def _drain(
        self,
        db: Connection,
        target: tuple[DirtySource, ...],
        deadline: float,
    ) -> tuple[list[DirtySource], list[str], list[str], bool]:
        changes = HistoryChanges(self.root)
        acknowledged: list[DirtySource] = []
        pending: list[str] = []
        active: list[str] = []
        busy = False
        for marker in target:
            if marker.source.kind == "sessions":
                try:
                    with lock_file(marker.source.lock_path(self.root), timeout=0):
                        # Only a lock owner may capture a token for acknowledgement.
                        current = DirtySource.model_validate_json(
                            (changes.directory / marker.source.name).read_bytes()
                        )
                        self._sessions(db)
                        acknowledged.append(current)
                except FileLockTimeout:
                    # The append-only journal is safe to read through its committed
                    # extent. Keep the marker so acknowledgement cannot race its writer.
                    self._sessions(db)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending.append(_source_label(marker.source))
                continue
            try:
                with lock_file(marker.source.lock_path(self.root), timeout=remaining):
                    # Capture the current token under the source lock; it may be newer.
                    current = DirtySource.model_validate_json(
                        (changes.directory / marker.source.name).read_bytes()
                    )
                    provisional = self._project(db, marker.source)
                    if provisional:
                        active.append(marker.source.key)
                    else:
                        acknowledged.append(current)
            except FileLockTimeout:
                busy = True
                pending.append(_source_label(marker.source))
        db.commit()
        return acknowledged, pending, active, busy

    def _reread_unmarked(self, db: Connection, target: tuple[DirtySource, ...]) -> list[str]:
        """Re-read what an older build's writers change without marking this queue.

        Each schema owns its marker queue. Runners and primaries of an older build
        that outlive an upgrade still finish spawns and append session events.
        Active loose spawns are provisional anyway, and the session log keeps a
        cursor, so both are cheap. Sessions project without their writer lock;
        busy spawn sources wait for the next catch-up. Spawns an older build creates
        after this projection was built need a rebuild.
        """
        marked = {marker.source.name for marker in target}
        sources = [
            HistorySource(kind="spawn", key=key)
            for (key,) in db.execute(
                select(RECORDS.c.local_id).where(
                    RECORDS.c.active == 1, RECORDS.c.archive_id.is_(None)
                )
            )
            if key
        ]
        sources.append(HistorySource(kind="sessions"))
        active: list[str] = []
        for source in sources:
            if source.name in marked:
                continue
            if source.kind == "sessions":
                self._sessions(db)  # Returns at once when nothing was appended.
                continue
            try:
                with lock_file(source.lock_path(self.root), timeout=0):
                    if self._spawn(db, source.key):
                        active.append(source.key)
            except FileLockTimeout:
                continue
        db.commit()
        return active

    def _rebuild_locked(
        self, *, reset: bool, deadline: float
    ) -> tuple[IndexCoverage, list[DirtySource]]:
        """Project and publish while the caller owns catchup and root mutation gates."""
        changes = HistoryChanges(self.root)
        if reset:
            # Full quiescent scan replaces unknown coordination; locks are never removed.
            with lock_file(changes.marker_lock, timeout=_remaining(deadline)):
                atomic_write_text(changes.directory / "GENERATION", str(uuid4()))
                for path in changes.directory.glob("*.json"):
                    path.unlink()
        generation, _ = changes.capture(timeout=_remaining(deadline))
        self.directory.mkdir(parents=True, exist_ok=True)
        stage = self.stage
        for suffix in ("", "-journal"):
            Path(str(stage) + suffix).unlink(missing_ok=True)
        try:
            build = str(uuid4())
            with _connect(stage, fresh=True, timeout=_remaining(deadline)) as db:
                INDEX_SCHEMA.create_all(db)
                db.execute(
                    insert(META).values(version=SCHEMA_VERSION, generation=generation, build=build)
                )
                for key in scan_spawn_ids(self.root / "spawns"):
                    with _source_lock(
                        HistorySource(kind="spawn", key=key).lock_path(self.root), deadline
                    ):
                        self._spawn(db, key)
                for kind in ("sessions", "catalog"):
                    source = HistorySource(kind=kind)
                    with _source_lock(source.lock_path(self.root), deadline):
                        self._project(db, source)
                _, target = changes.capture(timeout=_remaining(deadline))
                acknowledged, pending, active, busy = self._drain(db, target, deadline)
                if pending:
                    if busy:
                        raise FileLockTimeout("History sources are busy")
                    raise TimeoutError("History rebuild exhausted its remaining budget")
                db.execute(text("ANALYZE"))
                db.commit()
                self._warn_unsupported_sessions(db)
            # The root gate is already held: ordinary readers must never take it
            # while holding a database gate. Catchup/rebuild share catchup.lock.
            with lock_file(self.database_lock, timeout=_remaining(deadline)):
                if self.path.exists():
                    try:
                        with _connect(self.path, timeout=_remaining(deadline)) as old:
                            checkpoint = old.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).one()
                            if checkpoint[0]:
                                raise FileLockTimeout("Readers still own the old WAL")
                    except sqlite3.DatabaseError as exc:
                        # Only confirmed corruption is repairable here. Busy, I/O,
                        # permission and disk-full errors must leave the index alone.
                        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                        if code not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                            raise
                        preserved = self.directory / f"corrupt-{uuid4().hex}.sqlite3"
                        for suffix in ("", "-wal", "-shm"):
                            damaged = Path(str(self.path) + suffix)
                            if damaged.exists():
                                os.replace(damaged, Path(str(preserved) + suffix))
                        fsync_directory(self.directory)
                os.replace(stage, self.path)
                fsync_directory(self.directory)
        except BaseException:
            # Rename consumes the stage; successful publication needs no cleanup.
            # Failed cleanup must not hide the original error or latch cancellation.
            for suffix in ("", "-journal"):
                try:
                    Path(str(stage) + suffix).unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove unpublished history index staging file")
            raise
        return IndexCoverage(
            generation, build, True, activity_provisional=tuple(active)
        ), acknowledged

    def _clear_initialization_failure(self) -> tuple[str, ...]:
        """Called under catchup ownership when initialization may safely retry."""
        try:
            self.failure_path.unlink()
            fsync_directory(self.root)
        except FileNotFoundError:
            pass
        except OSError:
            warnings = (
                "Index published, but its initialization-failure marker could not be cleared.",
            )
            logger.warning(warnings[0])
            return warnings
        return ()

    def clear_authority_failure(self, *, timeout: float = 5.0) -> tuple[str, ...]:
        """Clear an authority failure after its source was repaired.

        Catch-up ownership orders this behind any initializer that may still
        publish a failure based on the pre-repair authority.
        """
        with lock_file(self.catchup_lock, timeout=timeout):
            try:
                failure = _InitializationFailure.model_validate_json(self.failure_path.read_bytes())
            except FileNotFoundError:
                return ()
            except ValueError:
                return ()
            if failure.code != "authority":
                return ()
            return self._clear_initialization_failure()

    def _finish_rebuild(
        self, coverage: IndexCoverage, acknowledged: list[DirtySource]
    ) -> IndexCoverage:
        # Publication has committed. Failures here must never latch initialization failure.
        warnings = self._clear_initialization_failure()
        for marker in acknowledged:
            HistoryChanges(self.root).acknowledge(marker)
        return IndexCoverage(
            coverage.generation,
            coverage.build,
            True,
            activity_provisional=coverage.activity_provisional,
            warnings=warnings,
        )

    def rebuild(self, *, reset: bool = False, timeout: float = 60) -> IndexCoverage:
        deadline = time.monotonic() + timeout
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(
                HistoryChanges(self.root).mutation_lock,
                mode="exclusive" if reset else "shared",
                timeout=_remaining(deadline),
            ),
        ):
            coverage, acknowledged = self._rebuild_locked(reset=reset, deadline=deadline)
            return self._finish_rebuild(coverage, acknowledged)

    @property
    def failure_path(self) -> Path:
        return self.root / f"{versioned('history-index-init-failure')}.json"

    def classify(self, *, deadline: float) -> IndexStatus:
        """Read schema/identity only; never create SQLite or alter its journal mode."""
        with lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)):
            if not self.path.exists():
                return IndexStatus("absent")
            try:
                with _connect(self.path, timeout=_remaining(deadline), readonly=True) as db:
                    row = db.execute(
                        select(META.c.version, META.c.generation, META.c.build)
                    ).first()
                    if row is None or not isinstance(row[0], int):
                        return IndexStatus("corrupt", reason="Missing or invalid index metadata")
                    version, generation, build = row
                    if version == SCHEMA_VERSION:
                        return IndexStatus("current", version, generation, build)
                    # Only this schema writes this file; anything else is a foreign copy.
                    reason = f"Index schema {version} does not match {self.path.name}."
                    return IndexStatus("incompatible", version, generation, build, reason)
            except sqlite3.DatabaseError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                    sqlite3.SQLITE_ERROR,
                }:
                    raise
                return IndexStatus("corrupt", reason="Unreadable index schema; rebuild required")

    def _failure_reason(self, generation: str | None) -> str | None:
        try:
            with self.failure_path.open("rb") as handle:
                data = handle.read(16 * 1024 + 1)
        except FileNotFoundError:
            return None
        try:
            if len(data) > 16 * 1024:
                raise ValueError("Oversized failure marker")
            failure = _InitializationFailure.model_validate_json(data)
        except ValueError:
            return "Corrupt initialization-failure marker"
        if failure.target_schema != SCHEMA_VERSION:
            return None
        if generation is not None and failure.generation not in {None, generation}:
            return None
        return failure.reason

    @staticmethod
    def _initialization_error(reason: str, *, persisted: bool = True) -> HistoryIndexIncomplete:
        suppression = (
            "Automatic initialization will not retry for this index generation."
            if persisted
            else "Could not persist failure suppression; automatic retries may recur."
        )
        return HistoryIndexIncomplete(
            f"History index initialization failed: {reason}. {suppression} Run: {_REBUILD_COMMAND}"
        )

    def _check_current(self, status: IndexStatus) -> None:
        if status.baseline != "current":
            raise HistoryIndexIncomplete(
                f"History index is {status.baseline}; initialization/rebuild required. "
                f"Run: {_REBUILD_COMMAND}"
            )
        if status.generation != HistoryChanges(self.root).read_generation():
            raise HistoryCoordinationError(
                "History baseline generation mismatch; run session index rebuild --reset"
            )

    def inspect(self, *, deadline: float | None = None) -> IndexStatus:
        deadline = time.monotonic() + QUERY_TIMEOUT if deadline is None else deadline
        status = self.classify(deadline=deadline)
        generation, _ = HistoryChanges(self.root).inspect(timeout=_remaining(deadline))
        if status.baseline == "current":
            self._check_current(status)
        elif status.baseline == "absent" and (reason := self._failure_reason(generation)):
            return IndexStatus(
                "failed",
                status.schema,
                generation,
                reason=str(self._initialization_error(reason)),
            )
        return status

    def initialize(self, *, deadline: float) -> IndexCoverage | None:
        """Explicit automatic-init phase; a corpus can share its deadline across roots."""
        changes = HistoryChanges(self.root)
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(changes.mutation_lock, mode="shared", timeout=_remaining(deadline)),
        ):
            status = self.classify(deadline=deadline)
            if status.baseline == "current":
                self._check_current(status)
                return None  # A peer already published while we waited.
            if status.baseline != "absent":
                self._check_current(status)
            generation = changes.read_generation()
            if reason := self._failure_reason(generation):
                raise self._initialization_error(reason)
            # Initialize absent coordination only under the normal protected gate.
            try:
                generation, _ = changes.capture(timeout=_remaining(deadline))
                coverage, acknowledged = self._rebuild_locked(reset=False, deadline=deadline)
            except (FileLockTimeout, HistoryCoordinationError):
                raise
            except (OSError, ValueError, sqlite3.Error) as exc:
                if isinstance(exc, sqlite3.Error) and (
                    getattr(exc, "sqlite_errorcode", 0) & 0xFF
                ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                if isinstance(exc, TimeoutError):
                    code, reason = (
                        "timeout",
                        "Metadata build exhausted its remaining initialization budget",
                    )
                elif isinstance(exc, OSError):
                    code, reason = "io", f"Metadata I/O failure ({type(exc).__name__})"
                elif isinstance(exc, sqlite3.Error):
                    code, reason = "sqlite", "SQLite metadata projection failed"
                elif isinstance(exc, SpawnStateQuarantined):
                    code = "authority"
                    reason = str(exc).splitlines()[0][:1024]
                else:
                    code, reason = "authority", "Invalid authoritative history metadata"
                failure = _InitializationFailure(
                    target_schema=SCHEMA_VERSION,
                    generation=generation,
                    code=code,
                    reason=reason,
                    failed_at=datetime.now(UTC).isoformat(),
                )
                try:
                    atomic_write_text(self.failure_path, failure.model_dump_json() + "\n")
                except OSError:
                    raise self._initialization_error(reason, persisted=False) from exc
                raise self._initialization_error(reason) from exc
            return self._finish_rebuild(coverage, acknowledged)

    def _ensure_baseline(self) -> None:
        """Build an absent index with its own sticky initialization budget."""
        if not self.path.exists():
            self.initialize(deadline=time.monotonic() + INITIALIZATION_TIMEOUT)

    @contextmanager
    def _translate_busy(self) -> Generator[None]:
        """Keep transient contention distinct from index corruption or incompleteness."""
        try:
            yield
        except HistoryIndexBusy:
            raise
        except FileLockTimeout as exc:
            gate = self._lock_timeout_gate(exc)
            names = (gate,) if gate else self._pending_source_names()
            raise self._busy_error(names) from exc
        except TimeoutError as exc:
            raise self._deadline_error(self._pending_source_names()) from exc

    def _current_catch_up(self, deadline: float) -> IndexCoverage:
        """One attempt; a non-current baseline fails closed instead of retrying."""
        self._check_current(self.classify(deadline=deadline))
        return self._catch_up(deadline)

    def catch_up(self, *, timeout: float | None = None) -> IndexCoverage:
        """Best-effort refresh; may be incomplete or raise ``HistoryIndexBusy``."""
        self._ensure_baseline()
        deadline = time.monotonic() + (QUERY_TIMEOUT if timeout is None else timeout)
        with self._translate_busy():
            return self._current_catch_up(deadline)

    def _catch_up(self, deadline: float) -> IndexCoverage:
        changes = HistoryChanges(self.root)
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(changes.mutation_lock, mode="shared", timeout=_remaining(deadline)),
            lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)),
        ):
            generation, target = changes.inspect(timeout=_remaining(deadline))
            if generation is None or not self.path.exists():
                raise self._busy_error(self._pending_source_names())
            with _connect(self.path, timeout=_remaining(deadline)) as db:
                meta = db.execute(select(META)).mappings().first()
                if meta is None or meta["generation"] != generation:
                    raise HistoryCoordinationError(
                        "History baseline generation mismatch; run session index rebuild --reset"
                    )
                if meta["version"] != SCHEMA_VERSION:
                    raise self._busy_error(self._pending_source_names())
                warnings = self._clear_initialization_failure()
                acknowledged, pending, active, _ = self._drain(db, target, deadline)
                active += self._reread_unmarked(db, target)
                for marker in acknowledged:
                    changes.acknowledge(marker)
                self._warn_unsupported_sessions(db)
                return IndexCoverage(
                    generation, meta["build"], not pending, tuple(pending), tuple(active), warnings
                )

    def _pending_source_names(self) -> tuple[str, ...]:
        try:
            _, pending = HistoryChanges(self.root).inspect(timeout=0)
        except FileLockTimeout:
            return ()
        return tuple(
            _source_label(marker.source) for marker in pending if marker.source.kind != "sessions"
        )

    @staticmethod
    def _source_names(sources: tuple[str, ...]) -> str:
        ordered = tuple(dict.fromkeys(sources))
        return ", ".join(ordered) if ordered else "history index coordination"

    def _lock_timeout_gate(self, exc: FileLockTimeout) -> str | None:
        if exc.path is None:
            return None
        path = exc.path.resolve()
        changes = HistoryChanges(self.root)
        gates = {
            self.catchup_lock.resolve(): "history index catch-up gate",
            changes.mutation_lock.resolve(): "history mutation gate",
            self.database_lock.resolve(): "history index database gate",
            changes.marker_lock.resolve(): "history marker gate",
            (self.root / "sessions.jsonl.flock").resolve(): "session writer gate",
            (self.root / "history-archives" / "catalog.lock").resolve(): "archive catalog gate",
        }
        if gate := gates.get(path):
            return gate
        if path.parent == (self.root / "locks" / "spawns").resolve():
            return "spawn source gate"
        return None

    def _busy_error(self, names: tuple[str, ...]) -> HistoryIndexBusy:
        formatted = self._source_names(names)
        return HistoryIndexBusy(
            f"History index is busy ({formatted}); another Meridian process holds it. "
            "Retry the command."
        )

    def _deadline_error(self, names: tuple[str, ...]) -> HistoryIndexBusy:
        formatted = self._source_names(names)
        return HistoryIndexBusy(
            "History index read did not obtain complete coverage before its deadline "
            f"({formatted}). Retry the command."
        )

    @contextmanager
    def query(self, *, deadline: float | None = None) -> Generator[Connection]:
        self._ensure_baseline()
        deadline = time.monotonic() + QUERY_TIMEOUT if deadline is None else deadline
        with self._translate_busy():
            coverage = self._current_catch_up(deadline)
            if not coverage.complete:
                raise self._deadline_error(coverage.pending)
        with ExitStack() as stack:
            with self._translate_busy():
                stack.enter_context(
                    lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline))
                )
                db = stack.enter_context(_connect(self.path, timeout=_remaining(deadline)))
            yield db

    def snapshots(self, *, destination: Path | None = None) -> tuple[HistorySnapshot, ...]:
        from meridian.lib.state.retention_archive import ArchiveReceipt, archive_display_path

        with self.query() as db:
            rows = (
                db.execute(
                    select(
                        LOCATIONS,
                        ARCHIVE_HEADS.c.portable_digest.label("current_digest"),
                        RECORDS.c.archive_id,
                    )
                    .select_from(
                        LOCATIONS.outerjoin(
                            ARCHIVE_HEADS,
                            ARCHIVE_HEADS.c.history_id == LOCATIONS.c.history_id,
                        ).outerjoin(RECORDS, RECORDS.c.history_id == LOCATIONS.c.history_id)
                    )
                    .where(LOCATIONS.c.kind == "archive")
                    .order_by(LOCATIONS.c.history_id, LOCATIONS.c.ordinal.desc())
                )
                .mappings()
                .all()
            )
        result: list[HistorySnapshot] = []
        for row in rows:
            receipt = ArchiveReceipt.model_validate_json(row["receipt_json"])
            result.append(
                HistorySnapshot(
                    history_id=row["history_id"],
                    archive_id=str(receipt.archive_id),
                    portable_digest=row["portable_digest"],
                    current=row["archive_id"] is not None
                    and row["portable_digest"] == row["current_digest"],
                    path=str(archive_display_path(receipt, destination)),
                )
            )
        return tuple(result)

    def spawns(
        self,
        *,
        oldest_first: bool = False,
        deadline: float | None = None,
        **filters: str | set[str] | None,
    ) -> tuple[SpawnRecord, ...]:
        columns = {
            "chat_id": RECORDS.c.chat,
            "owner_chat_id": RECORDS.c.owner,
            "parent_id": RECORDS.c.parent,
            "work_id": RECORDS.c.work,
            "status": RECORDS.c.status,
            "spawn_id": RECORDS.c.local_id,
            "kind": RECORDS.c.kind,
        }
        conditions: list[ColumnElement[bool]] = [RECORDS.c.archive_id.is_(None)]
        for key, value in filters.items():
            if key not in columns:
                raise ValueError(f"Unknown history filter: {key}")
            column = columns[key]
            if isinstance(value, set):
                conditions.append(column.in_(sorted(value)))
            elif value is not None:
                conditions.append(column == value)
        stmt = select(RECORDS.c.record_json).where(*conditions)
        stmt = (
            stmt.order_by(RECORDS.c.activity, RECORDS.c.history_id)
            if oldest_first
            else stmt.order_by(RECORDS.c.started.desc(), RECORDS.c.local_id.desc())
        )
        with self.query(deadline=deadline) as db:
            return tuple(SpawnRecord.model_validate_json(row[0]) for row in db.execute(stmt))

    def descendant_projection(
        self,
        root_spawn_id: str,
        *,
        active_only: bool = False,
        deadline: float | None = None,
    ) -> tuple[tuple[str, str | None, bool], ...]:
        """Return the indexed transitive subtree, retaining archived ancestry.

        The query context performs one bounded catch-up before taking the shared
        database lock.  Callers must authoritatively read every returned loose
        row before using its lifecycle state. ``active_only`` filters after
        traversal, preserving inactive ancestors needed to reach active descendants.
        """
        statement = text(
            """
            WITH RECURSIVE subtree(local_id, parent, archived, active, path) AS (
                SELECT local_id, parent, archive_id IS NOT NULL, active,
                       ',' || local_id || ','
                  FROM records
                 WHERE parent = :root AND local_id != :root
                UNION ALL
                SELECT child.local_id, child.parent,
                       child.archive_id IS NOT NULL, child.active,
                       subtree.path || child.local_id || ','
                  FROM records AS child
                  JOIN subtree ON child.parent = subtree.local_id
                 WHERE child.local_id != :root
                   AND instr(subtree.path, ',' || child.local_id || ',') = 0
            )
            SELECT local_id, parent, archived
              FROM subtree
             WHERE :active_only = 0 OR active = 1
             ORDER BY local_id
            """
        )
        with self.query(deadline=deadline) as db:
            return tuple(
                (str(row[0]), str(row[1]) if row[1] is not None else None, bool(row[2]))
                for row in db.execute(
                    statement, {"root": root_spawn_id, "active_only": active_only}
                )
            )

    def work_chat_ids(self, work_id: str, *, deadline: float | None = None) -> set[str]:
        with self.query(deadline=deadline) as db:
            return {
                row[0]
                for row in db.execute(
                    select(WORK_CHATS.c.chat)
                    .where(WORK_CHATS.c.work == work_id)
                    .union(select(RECORDS.c.chat).where(RECORDS.c.work == work_id))
                )
                if row[0]
            }

    def recent_sessions(
        self,
        *,
        limit: int,
        status: Literal["active", "archived"] | None = None,
        sort: Literal["updated", "created"] = "updated",
        chat_ids: set[str] | None = None,
        exclude_chat_ids: set[str] | None = None,
        query: str = "",
    ) -> BrowsePage:
        """Select picker records ordered by indexed activity or creation.

        ``chat_ids`` addresses specific chats and overrides ``status``;
        ``exclude_chat_ids`` drops chats the caller orders itself (running sessions, whose
        indexed activity is their start time). ``query`` keeps rows whose displayed chat,
        tmux target, agent, model (recorded or last executed), cwd or work (its name is its
        id) contains it, case-insensitively; it applies before LIMIT and to the total.
        """
        newer = SESSIONS.alias("newer")
        # The archive receipt's retained session, read once for ordering, matching and rows.
        archive_session = (
            select(func.json_extract(LOCATIONS.c.receipt_json, "$.records[0].session"))
            .join(ARCHIVE_HEADS, ARCHIVE_HEADS.c.history_id == LOCATIONS.c.history_id)
            .where(
                LOCATIONS.c.history_id == RECORDS.c.history_id,
                LOCATIONS.c.kind == "archive",
                LOCATIONS.c.portable_digest == ARCHIVE_HEADS.c.portable_digest,
            )
            .order_by(LOCATIONS.c.ordinal.desc(), LOCATIONS.c.source_id)
            .limit(1)
            .scalar_subquery()
        )

        def created(json: ColumnElement[str]) -> ColumnElement[str]:
            return func.coalesce(
                func.nullif(func.json_extract(json, "$.created_at"), ""),
                func.json_extract(json, "$.started_at"),
            )

        sessions = select(
            SESSIONS.c.record_json,
            SESSIONS.c.record_json.label("session_json"),
            literal("session").label("source"),
            func.json_extract(SESSIONS.c.record_json, "$.chat_id").label("chat"),
            SESSIONS.c.activity,
            created(SESSIONS.c.record_json).label("created"),
            func.cast(func.substr(SESSIONS.c.chat, 2), Integer).label("tie"),
        ).where(
            SESSIONS.c.kind == "primary",
            ~exists().where(newer.c.chat == SESSIONS.c.chat, newer.c.ordinal > SESSIONS.c.ordinal),
            ~exists().where(
                RECORDS.c.archive_id.is_not(None),
                RECORDS.c.history_id == SESSIONS.c.history_id,
            ),
        )
        archives = select(
            RECORDS.c.record_json,
            archive_session.label("session_json"),
            literal("archive").label("source"),
            RECORDS.c.history_id.label("chat"),
            RECORDS.c.activity,
            RECORDS.c.started,
            literal(0).label("tie"),
        ).where(RECORDS.c.archive_id.is_not(None), RECORDS.c.kind == "primary")
        archived = archives.subquery("archived_raw")
        browse = union_all(
            sessions,
            select(
                archived.c.record_json,
                archived.c.session_json,
                archived.c.source,
                archived.c.chat,
                archived.c.activity,
                func.coalesce(created(archived.c.session_json), archived.c.started).label(
                    "created"
                ),
                archived.c.tie,
            ),
        ).cte("browse")
        if chat_ids is not None:
            browse = (
                select(browse)
                .where(browse.c.source == "session", browse.c.chat.in_(sorted(chat_ids)))
                .cte("addressed")
            )
        else:
            conditions: list[ColumnElement[bool]] = []
            if status is not None:
                conditions.append(
                    browse.c.source == ("archive" if status == "archived" else "session")
                )
            if exclude_chat_ids:
                conditions.append(
                    or_(
                        browse.c.source != "session", browse.c.chat.not_in(sorted(exclude_chat_ids))
                    )
                )
            if conditions:
                browse = select(browse).where(*conditions).cte("scoped")
        needle = query.strip().lower()
        if needle:
            match = _metadata_match(
                browse.c.chat, browse.c.record_json, browse.c.session_json, needle
            )
            browse = select(browse).where(match).cte("matched")
        with self.query() as db:
            total = db.execute(select(func.count()).select_from(browse)).scalar() or 0
            rows = db.execute(
                select(browse.c.record_json, browse.c.source, browse.c.chat, browse.c.session_json)
                .order_by(
                    *(
                        (browse.c.activity.desc(), browse.c.tie.desc())
                        if sort == "updated"
                        else (func.julianday(browse.c.created).desc(), browse.c.tie.desc())
                    )
                )
                .limit(limit)
            )
            records: list[SessionRecord | SpawnRecord] = []
            archived_sessions: dict[str, SessionRecord] = {}
            for record_json, source, chat, session_json in rows:
                if source == "session":
                    records.append(SessionRecord.model_validate_json(record_json))
                    continue
                records.append(SpawnRecord.model_validate_json(record_json))
                if session_json is not None:
                    archived_sessions[chat] = SessionRecord.model_validate_json(session_json)
            return BrowsePage(records, total, archived_sessions)

    def last_executed_models(self, identities: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        if not identities:
            return {}
        with self.query() as db:
            return {
                (row[0], row[1]): row[2]
                for row in db.execute(
                    select(MODEL_OBSERVATIONS).where(
                        tuple_(MODEL_OBSERVATIONS.c.harness, MODEL_OBSERVATIONS.c.session).in_(
                            sorted(identities)
                        )
                    )
                )
            }

    def lineage_edges(self, chat_id: str) -> list[tuple[str, str, str]]:
        # Traverse both directions in SQLite, including multiple context parents.
        with self.query() as db:
            return [
                tuple(row)
                for row in db.exec_driver_sql(
                    "WITH RECURSIVE edges AS (SELECT * FROM session_lineage AS edge "
                    "WHERE NOT EXISTS (SELECT 1 FROM session_chats "
                    "WHERE chat = edge.parent AND json_extract(record_json, '$.kind') = 'spawn')), "
                    "tree(chat) AS (SELECT ? UNION "
                    "SELECT parent FROM edges JOIN tree ON child = chat UNION "
                    "SELECT child FROM edges JOIN tree ON parent = chat) "
                    "SELECT child, kind, parent FROM edges "
                    "WHERE child IN tree ORDER BY child, kind",
                    (chat_id,),
                )
            ]

    def sessions(
        self, *, limit: int | None = None, chat_ids: set[str] | None = None
    ) -> list[SessionRecord]:
        if chat_ids is not None and not chat_ids:
            return []
        stmt = select(SESSION_CHATS.c.record_json)
        if chat_ids is not None:
            stmt = stmt.where(SESSION_CHATS.c.chat.in_(sorted(chat_ids)))
        stmt = stmt.order_by(SESSION_CHATS.c.ordinal.desc())
        if limit is not None:
            if limit <= 0:
                raise ValueError("Session limit must be positive")
            stmt = stmt.limit(limit)
        with self.query() as db:
            return [SessionRecord.model_validate_json(row[0]) for row in db.execute(stmt)]


def indexed_spawn_scan(
    runtime_root: Path,
    *,
    parent_id: str | None = None,
    work_id: str | None = None,
    deadline: float | None = None,
) -> SpawnScan:
    """Discovery partition; corruption fails coverage rather than omitting a sibling."""
    records = HistoryIndex(runtime_root).spawns(
        parent_id=parent_id,
        work_id=work_id,
        deadline=deadline,
    )
    return SpawnScan.ordered(records, ())
