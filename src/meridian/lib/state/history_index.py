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
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
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
    union_all,
    update,
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
    DirtySource,
    HistoryChanges,
    HistoryCoordinationError,
    HistorySource,
)
from meridian.lib.state.history_codec import canonical_time, last_activity
from meridian.lib.state.session_store import (
    SessionHistoricalEvent,
    SessionRecord,
    SessionStartEvent,
    SessionUpdateEvent,
    _parse_event,
    project_session_event,
)
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import read_state, scan_spawn_ids

if TYPE_CHECKING:
    from meridian.lib.state.spawn_store import SpawnScan

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
INITIALIZATION_TIMEOUT = 15.0
QUERY_TIMEOUT = 2.0
_REBUILD_COMMAND = "uv run meridian session index rebuild --metadata-only"
type SchemaClass = Literal["expand", "reshape", "reproject"]
type SchemaUpgrade = Literal["migrate", "reproject"]

INDEX_SCHEMA = MetaData()
META = Table(
    "meta",
    INDEX_SCHEMA,
    Column("version", Integer, nullable=False),
    Column("generation", Text, nullable=False),
    Column("build", Text, nullable=False),
)
PREVIEWS = Table(
    "previews",
    INDEX_SCHEMA,
    Column("key", Text, primary_key=True),
    Column("history_id", Text),
    Column("archive_digest", Text),
    Column("value", Text, nullable=False),
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
)


@dataclass(frozen=True)
class SchemaStep:
    from_version: int
    to_version: int
    schema_class: SchemaClass
    apply: Callable[[Connection], None]


SCHEMA_STEPS: tuple[SchemaStep, ...] = ()


class HistoryIndexIncomplete(RuntimeError):
    """Discovery cannot safely promise complete candidate membership."""


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
    baseline: Literal["absent", "outdated", "current", "incompatible", "corrupt", "failed"]
    schema: int | None = None
    generation: str | None = None
    build: str | None = None
    reason: str | None = None
    upgrade: SchemaUpgrade | None = None


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


class HistoryReadTarget(NamedTuple):
    state: SpawnRecord
    path: Path
    archive_id: UUID | None = None
    manifest_sha256: str | None = None


class HistoryCandidate(NamedTuple):
    history_id: str
    local_id: str
    chat_id: str | None
    archived: bool
    activity: str


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("History index deadline exhausted")
    return remaining


def _schema_upgrade(live_version: int) -> tuple[SchemaUpgrade, tuple[SchemaStep, ...]]:
    """Plan the remaining chain. Missing or untrusted steps reproject."""
    if live_version < 2:
        return "reproject", ()
    ordered: dict[int, SchemaStep] = {}
    for step in SCHEMA_STEPS:
        if step.to_version != step.from_version + 1 or step.from_version in ordered:
            return "reproject", ()
        ordered[step.from_version] = step
    chain: list[SchemaStep] = []
    for version in range(live_version, SCHEMA_VERSION):
        step = ordered.get(version)
        if step is None:
            return "reproject", ()
        chain.append(step)
    if any(step.schema_class == "reproject" for step in chain):
        return "reproject", ()
    return "migrate", tuple(chain)


def _outdated_status(version: int) -> tuple[str, SchemaUpgrade]:
    upgrade, chain = _schema_upgrade(version)
    if upgrade == "reproject":
        return "metadata rebuild required (reproject)", upgrade
    shown = "reshape" if any(step.schema_class == "reshape" for step in chain) else "expand"
    return f"in-place migrate ({shown})", upgrade


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

        @event.listens_for(engine, "connect")
        def _configure(dbapi_connection: sqlite3.Connection, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=FULL")
                if not fresh:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

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


def _tail(path: Path, extent: int, count: int = 256) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, extent - count))
        return hashlib.sha256(handle.read(min(extent, count))).hexdigest()


def transcript_activity(path: Path, fallback: str) -> str:
    """Read the last complete event, expanding for large events rather than guessing age."""
    if not path.exists():
        return canonical_time(fallback)
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        window = min(end, 1024 * 1024)
        while True:
            handle.seek(end - window)
            chunk = handle.read(window)
            finish = chunk.rfind(b"\n")
            start = chunk.rfind(b"\n", 0, finish) + 1 if finish >= 0 else 0
            if finish >= 0 and (start or window == end):
                break
            if window == end:
                raise ValueError(f"Incomplete transcript tail: {path}")
            window = min(end, window * 2)
    event = json.loads(chunk[start:finish])
    if not isinstance(event, dict):
        raise ValueError(f"Transcript event is not an object: {path}")
    event = cast("dict[str, object]", event)
    stamp = event.get("timestamp", event.get("created_at", fallback))
    stamps = [value for value in (fallback, stamp) if isinstance(value, str) and value]
    return (
        max(datetime.fromisoformat(value).astimezone(UTC) for value in stamps).isoformat(
            timespec="microseconds"
        )
        if stamps
        else ""
    )


@dataclass(frozen=True)
class HistoryIndex:
    root: Path

    @property
    def directory(self) -> Path:
        return self.root / "history-index"

    @property
    def path(self) -> Path:
        return self.directory / "history.sqlite3"

    @property
    def catchup_lock(self) -> Path:
        return self.root / "locks" / "history-catchup.lock"

    @property
    def database_lock(self) -> Path:
        return self.root / "locks" / "history-database.lock"

    def _aliases(
        self,
        db: Connection,
        source_id: str,
        history_id: str,
        state: SpawnRecord | None = None,
        session: SessionRecord | None = None,
    ) -> None:
        db.execute(delete(ALIASES).where(ALIASES.c.source_id == source_id))
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
            if session.spawn_id:
                names.add((session.spawn_id, "spawn"))
            names.update((name, "harness") for name in session.harness_session_ids)
            if session.harness_session_id:
                names.add((session.harness_session_id, "harness"))
        rows = [
            {
                "source_id": source_id,
                "alias": name,
                "kind": kind,
                "history_id": history_id,
            }
            for name, kind in names
        ]
        if rows:
            db.execute(insert(ALIASES).prefix_with("OR IGNORE"), rows)

    def _refresh(self, db: Connection, history_id: str) -> None:
        head_digest = (
            select(ARCHIVE_HEADS.c.portable_digest)
            .where(ARCHIVE_HEADS.c.history_id == LOCATIONS.c.history_id)
            .scalar_subquery()
        )
        location = (
            db.execute(
                select(LOCATIONS)
                .where(
                    LOCATIONS.c.history_id == history_id,
                    or_(
                        LOCATIONS.c.kind == "spawn",
                        LOCATIONS.c.portable_digest == head_digest,
                    ),
                )
                .order_by(
                    (LOCATIONS.c.kind == "spawn").desc(),
                    LOCATIONS.c.ordinal.desc(),
                    LOCATIONS.c.source_id,
                )
                .limit(1)
            )
            .mappings()
            .first()
        )
        db.execute(delete(RECORDS).where(RECORDS.c.history_id == history_id))
        if location is None:
            return
        record = SpawnRecord.model_validate_json(location["record_json"])
        receipt = json.loads(location["receipt_json"]) if location["receipt_json"] else None
        active = record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES
        session = None
        if location["kind"] == "spawn":
            related = db.execute(
                select(SESSIONS.c.record_json)
                .where(
                    or_(
                        SESSIONS.c.history_id == history_id,
                        and_(
                            SESSIONS.c.chat == record.chat_id,
                            SESSIONS.c.generation == record.session_instance_id,
                        ),
                    )
                )
                .order_by(SESSIONS.c.activity.desc())
                .limit(1)
            ).first()
            if related:
                session = SessionRecord.model_validate_json(related[0])
        activity = last_activity(record, session, location["activity"])
        db.execute(
            insert(RECORDS).values(
                history_id=history_id,
                local_id=record.id,
                chat=record.chat_id,
                owner=record.owner_chat_id or record.chat_id,
                parent=record.parent_id,
                work=record.work_id,
                status=str(record.status),
                kind=record.kind,
                started=canonical_time(record.started_at or ""),
                activity=activity,
                active=int(active and receipt is None),
                archive_id=receipt["archive_id"] if receipt else None,
                record_json=record.model_dump_json(),
            )
        )

    def _spawn(self, db: Connection, key: str) -> bool:
        source_id = f"spawn:{key}"
        old = db.execute(
            select(LOCATIONS.c.history_id).where(LOCATIONS.c.source_id == source_id)
        ).first()
        db.execute(delete(LOCATIONS).where(LOCATIONS.c.source_id == source_id))
        db.execute(delete(ALIASES).where(ALIASES.c.source_id == source_id))
        if old:
            self._refresh(db, old[0])
        record = read_state(self.root / "spawns", key, include_prompt=False)
        if record is None:
            return False
        history_id = str(record.history_id or uuid5(NAMESPACE_URL, f"{self.root}:{key}"))
        if db.execute(
            select(LOCATIONS.c.source_id).where(
                LOCATIONS.c.history_id == history_id, LOCATIONS.c.kind == "spawn"
            )
        ).first():
            raise ValueError(f"Conflicting loose copies of history {history_id}")
        activity = transcript_activity(
            self.root / "spawns" / key / "history.jsonl",
            record.terminal.finished_at if record.terminal else record.started_at or "",
        )
        db.execute(
            insert(LOCATIONS).values(
                source_id=source_id,
                history_id=history_id,
                kind="spawn",
                ordinal=0,
                activity=activity,
                record_json=record.model_dump_json(),
                receipt_json=None,
                portable_digest=None,
            )
        )
        self._aliases(db, source_id, history_id, state=record)
        self._refresh(db, history_id)
        return record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES

    def _sessions(self, db: Connection) -> None:
        path = self.root / "sessions.jsonl"
        if not path.exists():
            db.execute(delete(SESSIONS))
            db.execute(delete(WORK_CHATS))
            db.execute(delete(ALIASES).where(ALIASES.c.source_id.like("session:%")))
            db.execute(delete(CURSORS).where(CURSORS.c.source == "sessions"))
            return
        stat = path.stat()
        inode = f"{stat.st_dev}:{stat.st_ino}"
        cursor = (
            db.execute(select(CURSORS).where(CURSORS.c.source == "sessions")).mappings().first()
        )
        offset = 0
        if (
            cursor
            and cursor["inode"] == inode
            and cursor["extent"] <= stat.st_size
            and cursor["tail"] == _tail(path, cursor["extent"])
        ):
            offset = cursor["extent"]
        else:
            db.execute(delete(SESSIONS))
            db.execute(delete(WORK_CHATS))
            db.execute(delete(ALIASES).where(ALIASES.c.source_id.like("session:%")))
        with path.open("rb") as handle:
            handle.seek(offset)
            while line := handle.readline():
                if not line.endswith(b"\n"):
                    break
                end = handle.tell()
                try:
                    payload = json.loads(line)
                    # Match the authoritative event reader's tolerant line policy.
                    event = (
                        _parse_event(cast("dict[str, Any]", payload))
                        if isinstance(payload, dict)
                        else None
                    )
                except (ValueError, UnicodeDecodeError):
                    offset = end
                    continue
                if isinstance(event, SessionUpdateEvent) and event.active_work_id:
                    db.execute(
                        insert(WORK_CHATS)
                        .prefix_with("OR IGNORE")
                        .values(work=event.active_work_id.strip(), chat=event.chat_id)
                    )
                if event is not None:
                    generation = event.session_instance_id
                    if not generation:
                        if isinstance(event, (SessionStartEvent, SessionHistoricalEvent)):
                            generation = f"legacy:{offset}"
                        else:
                            latest = db.execute(
                                select(SESSIONS.c.generation)
                                .where(
                                    SESSIONS.c.chat == event.chat_id,
                                    SESSIONS.c.generation.like("legacy:%"),
                                )
                                .order_by(SESSIONS.c.ordinal.desc())
                                .limit(1)
                            ).first()
                            generation = latest[0] if latest else ""
                    found = (
                        db.execute(
                            select(SESSIONS).where(
                                SESSIONS.c.chat == event.chat_id,
                                SESSIONS.c.generation == generation,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    records: dict[str, SessionRecord] = {}
                    if found:
                        records[event.chat_id] = SessionRecord.model_validate_json(
                            found["record_json"]
                        )
                    project_session_event(records, event)
                    if record := records.get(event.chat_id):
                        history_id = str(record.history_id) if record.history_id else None
                        if history_id is None and record.spawn_id:
                            linked = db.execute(
                                select(LOCATIONS.c.history_id).where(
                                    LOCATIONS.c.source_id == f"spawn:{record.spawn_id}"
                                )
                            ).first()
                            history_id = linked[0] if linked else None
                        if history_id is None and generation:
                            linked = db.execute(
                                select(RECORDS.c.history_id)
                                .where(
                                    RECORDS.c.chat == record.chat_id,
                                    RECORDS.c.archive_id.is_(None),
                                    func.json_extract(
                                        RECORDS.c.record_json, "$.session_instance_id"
                                    )
                                    == generation,
                                )
                                .limit(1)
                            ).first()
                            history_id = linked[0] if linked else None
                        ordinal = (
                            offset
                            if isinstance(event, (SessionStartEvent, SessionHistoricalEvent))
                            else (found["ordinal"] if found else offset)
                        )
                        db.execute(
                            insert(SESSIONS)
                            .prefix_with("OR REPLACE")
                            .values(
                                chat=record.chat_id,
                                generation=generation,
                                ordinal=ordinal,
                                kind=record.kind,
                                stopped=record.stopped_at,
                                activity=canonical_time(record.stopped_at or record.started_at),
                                history_id=history_id,
                                record_json=record.model_dump_json(),
                            )
                        )
                        if history_id:
                            self._aliases(
                                db,
                                f"session:{record.chat_id}:{generation}",
                                history_id,
                                session=record,
                            )
                            self._refresh(db, history_id)
                offset = end
        db.execute(
            insert(CURSORS)
            .prefix_with("OR REPLACE")
            .values(source="sessions", inode=inode, extent=offset, tail=_tail(path, offset))
        )

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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pending.append(marker.source.name)
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
                pending.append(marker.source.name)
        db.commit()
        return acknowledged, pending, active, busy

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
        # catchup_lock owns this disposable stage, including crash residue.
        stage = self.directory / ".build.sqlite3"
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
                    with lock_file(
                        HistorySource(kind="spawn", key=key).lock_path(self.root),
                        timeout=_remaining(deadline),
                    ):
                        self._spawn(db, key)
                for kind in ("sessions", "catalog"):
                    source = HistorySource(kind=kind)
                    with lock_file(source.lock_path(self.root), timeout=_remaining(deadline)):
                        self._project(db, source)
                _, target = changes.capture(timeout=_remaining(deadline))
                acknowledged, pending, active, busy = self._drain(db, target, deadline)
                if pending:
                    if busy:
                        raise FileLockTimeout("History sources are busy")
                    raise TimeoutError("History rebuild exhausted its remaining budget")
                db.execute(text("ANALYZE"))
                db.commit()
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

    def _migrate_locked(self, *, deadline: float) -> None:
        """Apply expand/reshape steps on the live WAL DB. Caller owns catchup/root."""
        with lock_file(self.database_lock, timeout=_remaining(deadline)):
            status = self.classify(deadline=deadline)
            if (
                status.baseline != "outdated"
                or status.upgrade != "migrate"
                or status.schema is None
            ):
                return
            _, chain = _schema_upgrade(status.schema)
            if not chain:
                return
            with _connect(self.path, timeout=_remaining(deadline), autocommit=True) as db:
                for step in chain:
                    _remaining(deadline)
                    db.exec_driver_sql("BEGIN IMMEDIATE")
                    try:
                        step.apply(db)
                        db.execute(update(META).values(version=step.to_version))
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise

    def _clear_initialization_failure(self) -> tuple[str, ...]:
        """Called under catchup ownership after verifying a compatible published baseline."""
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
        return self.root / "history-index-init-failure.json"

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
                    baseline = (
                        "current"
                        if version == SCHEMA_VERSION
                        else "outdated"
                        if version < SCHEMA_VERSION
                        else "incompatible"
                    )
                    reason: str | None = None
                    upgrade: SchemaUpgrade | None = None
                    if baseline == "incompatible":
                        reason = (
                            "Index schema "
                            f"{version} is newer than supported schema {SCHEMA_VERSION}."
                        )
                    elif baseline == "outdated":
                        reason, upgrade = _outdated_status(version)
                    return IndexStatus(baseline, version, generation, build, reason, upgrade)
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
        elif status.baseline in {"absent", "outdated"} and (
            reason := self._failure_reason(generation)
        ):
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
            if status.baseline not in {"absent", "outdated"}:
                self._check_current(status)
            generation = changes.read_generation()
            if reason := self._failure_reason(generation):
                raise self._initialization_error(reason)
            # Initialize absent coordination only under the normal protected gate.
            try:
                if (
                    status.baseline == "outdated"
                    and status.upgrade == "migrate"
                    and status.generation == generation
                ):
                    self._migrate_locked(deadline=deadline)
                    self._clear_initialization_failure()
                    return None
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

    def _operation_deadline(self, deadline: float | None) -> float:
        ordinary = time.monotonic() + QUERY_TIMEOUT if deadline is None else deadline
        status = self.classify(deadline=ordinary)
        if status.baseline in {"absent", "outdated"} and deadline is None:
            self.initialize(deadline=time.monotonic() + INITIALIZATION_TIMEOUT)
            return time.monotonic() + QUERY_TIMEOUT
        self._check_current(status)
        return ordinary

    def catch_up(self, *, timeout: float | None = None) -> IndexCoverage:
        deadline = self._operation_deadline(None if timeout is None else time.monotonic() + timeout)
        return self._catch_up(deadline)

    def _catch_up(self, deadline: float) -> IndexCoverage:
        changes = HistoryChanges(self.root)
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(changes.mutation_lock, mode="shared", timeout=_remaining(deadline)),
            lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)),
        ):
            generation, target = changes.inspect(timeout=_remaining(deadline))
            if generation is None or not self.path.exists():
                raise HistoryIndexIncomplete(
                    "History index changed after preflight; retry required"
                )
            with _connect(self.path, timeout=_remaining(deadline)) as db:
                meta = db.execute(select(META)).mappings().first()
                if meta is None or meta["generation"] != generation:
                    raise HistoryCoordinationError(
                        "History baseline generation mismatch; run session index rebuild --reset"
                    )
                if meta["version"] != SCHEMA_VERSION:
                    raise HistoryIndexIncomplete("History index schema changed after preflight")
                warnings = self._clear_initialization_failure()
                acknowledged, pending, active, _ = self._drain(db, target, deadline)
                for marker in acknowledged:
                    changes.acknowledge(marker)
                return IndexCoverage(
                    generation, meta["build"], not pending, tuple(pending), tuple(active), warnings
                )

    @contextmanager
    def query(self, *, deadline: float | None = None) -> Generator[Connection]:
        deadline = self._operation_deadline(deadline)
        coverage = self._catch_up(deadline)
        if not coverage.complete:
            raise HistoryIndexIncomplete(
                f"History index has unresolved sources: {coverage.pending}"
            )
        with (
            lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)),
            _connect(self.path, timeout=_remaining(deadline)) as db,
        ):
            yield db

    def preview_references(self) -> tuple[tuple[str, str | None, str], ...]:
        recent, _ = self.recent_sessions(limit=2**31 - 1, live_chat_ids=set())
        references: list[tuple[str, str | None, str]] = []
        seen: set[str] = set()
        for row in recent:
            history_id = str(row.history_id) if row.history_id else None
            if isinstance(row, SpawnRecord):
                ref, generation = str(row.history_id), row.session_instance_id or ""
            else:
                ref = row.chat_id
                generation = row.session_instance_id or row.harness_session_id or row.started_at
            references.append((ref, history_id, generation))
            if history_id:
                seen.add(history_id)
        with self.query() as db:
            # Warming includes selected archived children, not only loose spawns
            # and the primary-session browser's rows.
            for history_id, generation in db.execute(
                select(
                    RECORDS.c.history_id,
                    func.json_extract(RECORDS.c.record_json, "$.session_instance_id"),
                )
            ):
                if history_id not in seen:
                    references.append((history_id, history_id, generation or ""))
        return tuple(references)

    def preview_count(self, *, preview_version: int, deadline: float | None = None) -> int:
        """Read cached counts without initializing or catching up metadata."""
        deadline = time.monotonic() + QUERY_TIMEOUT if deadline is None else deadline
        with lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)):
            if not self.path.exists():
                return 0
            with _connect(self.path, timeout=_remaining(deadline), readonly=True) as db:
                meta = db.execute(select(META.c.version)).first()
                if meta is None or meta[0] != SCHEMA_VERSION:
                    return 0
                preview_rows = select(
                    PREVIEWS.c.key, PREVIEWS.c.history_id, PREVIEWS.c.archive_digest
                ).where(
                    func.json_valid(PREVIEWS.c.value) == 1,
                    func.json_extract(PREVIEWS.c.value, "$.preview.version") == preview_version,
                    func.json_extract(PREVIEWS.c.value, "$.preview.rendering_reason").is_(None),
                    func.json_extract(PREVIEWS.c.value, "$.complete") == 1,
                )
                return sum(
                    self._preview_generation_matches(db, row[0])
                    and self._preview_binding_matches(db, row[1], row[2])
                    for row in db.execute(preview_rows)
                )

    def preview_cache(self, key: str) -> tuple[str, str | None] | None:
        """Bounded cache-only lookup: never catch up metadata or open source content."""
        if not self.path.exists():
            return None
        with (
            lock_file(self.database_lock, mode="shared", timeout=0.005),
            _connect(self.path, timeout=0.005, readonly=True) as db,
        ):
            meta = db.execute(select(META.c.version, META.c.build)).first()
            if meta is None or meta[0] != SCHEMA_VERSION:
                return None
            row = db.execute(
                select(PREVIEWS.c.history_id, PREVIEWS.c.archive_digest, PREVIEWS.c.value).where(
                    PREVIEWS.c.key == key
                )
            ).first()
            if (
                row is not None
                and self._preview_generation_matches(db, key)
                and self._preview_binding_matches(db, row[0], row[1])
            ):
                return meta[1], row[2]
            return meta[1], None

    @staticmethod
    def _preview_generation_matches(db: Connection, key: str) -> bool:
        history_id, generation, ref = json.loads(key)
        if history_id is not None:
            return True  # The portable UUID, not a reusable alias, resolves this source.
        if not generation:
            return False
        row = db.execute(
            select(SESSIONS.c.record_json)
            .where(SESSIONS.c.chat == ref)
            .order_by(SESSIONS.c.ordinal.desc())
            .limit(1)
        ).first()
        if row is None:
            return False
        session = SessionRecord.model_validate_json(row[0])
        return generation == (
            session.session_instance_id or session.harness_session_id or session.started_at
        )

    @staticmethod
    def _preview_binding_matches(
        db: Connection, history_id: str | None, archive_digest: str | None
    ) -> bool:
        if history_id is None:
            return archive_digest is None
        row = db.execute(
            select(RECORDS.c.archive_id, ARCHIVE_HEADS.c.portable_digest)
            .select_from(
                RECORDS.outerjoin(ARCHIVE_HEADS, ARCHIVE_HEADS.c.history_id == RECORDS.c.history_id)
            )
            .where(RECORDS.c.history_id == history_id)
        ).first()
        if row is None:
            return False
        return (
            row[0] is None
            if archive_digest is None
            else row[0] is not None and row[1] == archive_digest
        )

    def selected_archive_digest(self, history_id: str) -> str | None:
        with self.query() as db:
            row = db.execute(
                select(ARCHIVE_HEADS.c.portable_digest).where(
                    ARCHIVE_HEADS.c.history_id == history_id
                )
            ).first()
            return row[0] if row else None

    def store_preview(
        self,
        key: str,
        *,
        build: str,
        previous: str | None,
        history_id: str | None,
        archive_digest: str | None,
        source: HistorySource,
        prepare_value: Callable[[], str | None],
    ) -> bool:
        """Publish bounded derived content only into the generation that requested it."""
        with (
            lock_file(HistoryChanges(self.root).mutation_lock, mode="shared", timeout=0.1),
            lock_file(self.database_lock, mode="shared", timeout=0.1),
            lock_file(source.lock_path(self.root), timeout=0.1),
            _connect(self.path, timeout=0.1, autocommit=True) as db,
        ):
            db.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                meta = db.execute(select(META.c.build)).first()
                if (
                    meta is None
                    or meta[0] != build
                    or not self._preview_generation_matches(db, key)
                    or not self._preview_binding_matches(db, history_id, archive_digest)
                ):
                    db.rollback()
                    return False
                old = db.execute(
                    select(
                        PREVIEWS.c.history_id, PREVIEWS.c.archive_digest, PREVIEWS.c.value
                    ).where(PREVIEWS.c.key == key)
                ).first()
                if (old[2] if old else None) != previous and (
                    old is None or self._preview_binding_matches(db, old[0], old[1])
                ):
                    db.rollback()
                    return False
                value = prepare_value()
                if value is None:
                    db.rollback()
                    return False
                if len(value.encode("utf-8")) > 64 * 1024:
                    raise ValueError("Preview exceeds the bounded cache contract")
                db.execute(
                    insert(PREVIEWS)
                    .prefix_with("OR REPLACE")
                    .values(
                        key=key,
                        history_id=history_id,
                        archive_digest=archive_digest,
                        value=value,
                    )
                )
                db.commit()
                return True
            except BaseException:
                db.rollback()
                raise

    def read_targets(
        self, ref: str, *, destination: Path | None = None, deadline: float | None = None
    ) -> tuple[HistoryReadTarget, ...]:
        from meridian.lib.state.retention_archive import ArchiveReceipt, archive_locations

        deadline = self._operation_deadline(deadline)
        with self.query(deadline=deadline) as db:
            direct = db.execute(
                select(RECORDS.c.history_id).where(RECORDS.c.history_id == ref)
            ).first()
            if direct:
                history_id = direct[0]
            else:
                kind = (
                    "spawn"
                    if ref.startswith("p") and ref[1:].isdigit()
                    else ("chat" if ref.startswith("c") and ref[1:].isdigit() else "harness")
                )
                matches = (
                    db.execute(
                        select(
                            RECORDS.c.history_id,
                            func.max(ALIASES.c.source_id.not_like("archive:%")).label("local"),
                        )
                        .select_from(
                            ALIASES.join(RECORDS, ALIASES.c.history_id == RECORDS.c.history_id)
                        )
                        .where(ALIASES.c.alias == ref, ALIASES.c.kind == kind)
                        .group_by(RECORDS.c.history_id)
                        .order_by(
                            func.max(ALIASES.c.source_id.not_like("archive:%")).desc(),
                            (RECORDS.c.kind == "primary").desc(),
                            RECORDS.c.started.desc(),
                        )
                    )
                    .mappings()
                    .all()
                )
                if not matches:
                    return ()
                if len(matches) > 1 and not matches[0]["local"]:
                    raise ValueError("Ambiguous archive origin alias; use a portable history UUID")
                history_id = matches[0]["history_id"]
            head_digest = (
                select(ARCHIVE_HEADS.c.portable_digest)
                .where(ARCHIVE_HEADS.c.history_id == LOCATIONS.c.history_id)
                .scalar_subquery()
            )
            locations = (
                db.execute(
                    select(LOCATIONS)
                    .join(RECORDS, RECORDS.c.history_id == LOCATIONS.c.history_id)
                    .where(
                        LOCATIONS.c.history_id == history_id,
                        or_(
                            LOCATIONS.c.kind == "spawn",
                            and_(
                                RECORDS.c.archive_id.is_not(None),
                                LOCATIONS.c.portable_digest == head_digest,
                            ),
                        ),
                    )
                    .order_by(
                        (LOCATIONS.c.kind == "spawn").desc(),
                        LOCATIONS.c.ordinal.desc(),
                    )
                )
                .mappings()
                .all()
            )
        receipts: list[ArchiveReceipt] = []
        for location in locations:
            state = SpawnRecord.model_validate_json(location["record_json"])
            if location["kind"] == "spawn":
                from meridian.lib.state.native_snapshot import NATIVE_SNAPSHOT_FILENAME
                from meridian.lib.state.paths import resolve_spawn_output_path

                transcript = resolve_spawn_output_path(self.root, state.id)
                if transcript is not None and transcript.name == NATIVE_SNAPSHOT_FILENAME:
                    return (HistoryReadTarget(state, transcript),)
                if transcript is not None:
                    from meridian.lib.state.history_codec import TranscriptHeader

                    with transcript.open("rb") as handle:
                        first = handle.readline(1024 * 1024)
                    try:
                        header = json.loads(first)
                    except ValueError:
                        header = None
                    if (
                        isinstance(header, dict)
                        and header.get("record") == "meridian.transcript"
                        and TranscriptHeader.model_validate(header).history_id != state.history_id
                    ):
                        raise ValueError("Transcript identity does not match its record")
                    return (HistoryReadTarget(state, transcript),)
                if state.status not in TERMINAL_SPAWN_STATUSES:
                    return ()
                continue
            receipts.append(ArchiveReceipt.model_validate_json(location["receipt_json"]))
        if not receipts:
            return ()
        return tuple(
            HistoryReadTarget(
                location.receipt.records[0].state,
                location.path,
                location.receipt.archive_id,
                location.receipt.manifest_sha256,
            )
            for location in archive_locations(receipts, destination=destination, deadline=deadline)
        )

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

    def candidates(
        self, *, include_archives: bool = False, deadline: float | None = None
    ) -> tuple[HistoryCandidate, ...]:
        with self.query(deadline=deadline) as db:
            record_rows = select(
                RECORDS.c.history_id,
                RECORDS.c.local_id,
                RECORDS.c.chat,
                RECORDS.c.archive_id,
                RECORDS.c.activity,
            )
            if not include_archives:
                record_rows = record_rows.where(RECORDS.c.archive_id.is_(None))
            newer = SESSIONS.alias("newer")
            session_rows = select(
                SESSIONS.c.chat.label("history_id"),
                SESSIONS.c.chat.label("local_id"),
                SESSIONS.c.chat.label("chat"),
                literal(None).label("archive_id"),
                SESSIONS.c.activity,
            ).where(
                ~exists().where(RECORDS.c.chat == SESSIONS.c.chat),
                ~exists().where(
                    newer.c.chat == SESSIONS.c.chat, newer.c.ordinal > SESSIONS.c.ordinal
                ),
            )
            rows = db.execute(
                union_all(record_rows, session_rows).order_by(
                    text("activity DESC"), text("history_id")
                )
            )
            return tuple(
                HistoryCandidate(row[0], row[1], row[2], row[3] is not None, row[4]) for row in rows
            )

    def spawns(
        self,
        *,
        oldest_first: bool = False,
        related_chat_ids: set[str] | None = None,
        **filters: str | set[str] | None,
    ) -> tuple[SpawnRecord, ...]:
        columns = {
            "chat_id": RECORDS.c.chat,
            "owner_chat_id": RECORDS.c.owner,
            "parent_id": RECORDS.c.parent,
            "work_id": RECORDS.c.work,
            "status": RECORDS.c.status,
            "spawn_id": RECORDS.c.local_id,
        }
        conditions: list[ColumnElement[bool]] = [RECORDS.c.archive_id.is_(None)]
        if related_chat_ids is not None:
            chats = sorted(related_chat_ids)
            conditions.append(or_(RECORDS.c.chat.in_(chats), RECORDS.c.owner.in_(chats)))
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
        with self.query() as db:
            return tuple(SpawnRecord.model_validate_json(row[0]) for row in db.execute(stmt))

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
        self, *, limit: int, live_chat_ids: set[str]
    ) -> tuple[list[SessionRecord | SpawnRecord], int]:
        newer = SESSIONS.alias("newer")
        live_expr = SESSIONS.c.chat.in_(sorted(live_chat_ids)) if live_chat_ids else literal(0)
        browse = union_all(
            select(
                SESSIONS.c.record_json,
                literal("session").label("source"),
                SESSIONS.c.activity,
                live_expr.label("live"),
                func.cast(func.substr(SESSIONS.c.chat, 2), Integer).label("tie"),
            ).where(
                SESSIONS.c.kind == "primary",
                ~exists().where(
                    newer.c.chat == SESSIONS.c.chat, newer.c.ordinal > SESSIONS.c.ordinal
                ),
                ~exists().where(
                    RECORDS.c.archive_id.is_not(None),
                    RECORDS.c.history_id == SESSIONS.c.history_id,
                ),
            ),
            select(
                RECORDS.c.record_json,
                literal("archive").label("source"),
                RECORDS.c.activity,
                literal(0).label("live"),
                literal(0).label("tie"),
            ).where(RECORDS.c.archive_id.is_not(None), RECORDS.c.kind == "primary"),
        ).cte("browse")
        with self.query() as db:
            total = db.execute(select(func.count()).select_from(browse)).scalar() or 0
            rows = db.execute(
                select(browse.c.record_json, browse.c.source)
                .order_by(browse.c.live.desc(), browse.c.activity.desc(), browse.c.tie.desc())
                .limit(limit)
            )
            return [
                SessionRecord.model_validate_json(row[0])
                if row[1] == "session"
                else SpawnRecord.model_validate_json(row[0])
                for row in rows
            ], total

    def sessions(
        self, *, limit: int | None = None, chat_ids: set[str] | None = None
    ) -> list[SessionRecord]:
        if chat_ids is not None and not chat_ids:
            return []
        newer = SESSIONS.alias("newer")
        stmt = select(SESSIONS.c.record_json).where(
            ~exists().where(newer.c.chat == SESSIONS.c.chat, newer.c.ordinal > SESSIONS.c.ordinal)
        )
        if chat_ids is not None:
            stmt = stmt.where(SESSIONS.c.chat.in_(sorted(chat_ids)))
        stmt = stmt.order_by(SESSIONS.c.ordinal.desc())
        if limit is not None:
            if limit <= 0:
                raise ValueError("Session limit must be positive")
            stmt = stmt.limit(limit)
        with self.query() as db:
            return [SessionRecord.model_validate_json(row[0]) for row in db.execute(stmt)]


def indexed_spawn_scan(
    runtime_root: Path,
    *,
    chat_id: str | None = None,
    related_chat_ids: set[str] | None = None,
    owner_chat_id: str | set[str] | None = None,
    parent_id: str | None = None,
    work_id: str | None = None,
) -> SpawnScan:
    """Discovery partition; corruption fails coverage rather than omitting a sibling."""
    from meridian.lib.state.spawn_store import SpawnScan, _spawn_sort_key

    records = HistoryIndex(runtime_root).spawns(
        chat_id=chat_id,
        related_chat_ids=related_chat_ids,
        owner_chat_id=owner_chat_id,
        parent_id=parent_id,
        work_id=work_id,
    )
    return SpawnScan(tuple(sorted(records, key=_spawn_sort_key)), ())
