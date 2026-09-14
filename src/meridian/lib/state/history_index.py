"""One disposable metadata projection, shared by history discovery surfaces.

Writers only invalidate sources. This module alone projects authoritative files
and acknowledges changes. Lifecycle decisions must still read the stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.platform.locking import lock_file
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

_SCHEMA = """
CREATE TABLE meta(version INTEGER NOT NULL, generation TEXT NOT NULL, build TEXT NOT NULL);
CREATE TABLE previews(
 key TEXT PRIMARY KEY, history_id TEXT, archive_digest TEXT, value TEXT NOT NULL
);
CREATE TABLE records(
 history_id TEXT PRIMARY KEY, local_id TEXT, chat TEXT, owner TEXT, parent TEXT,
 work TEXT, status TEXT NOT NULL, kind TEXT NOT NULL, started TEXT NOT NULL,
 activity TEXT NOT NULL, active INTEGER NOT NULL, archive_id TEXT,
 record_json TEXT NOT NULL
);
CREATE UNIQUE INDEX loose_alias ON records(local_id) WHERE archive_id IS NULL;
CREATE INDEX owner_records ON records(owner,started);
CREATE INDEX chat_records ON records(chat,started);
CREATE INDEX parent_records ON records(parent);
CREATE INDEX work_records ON records(work,started);
CREATE INDEX status_records ON records(status,started);
CREATE INDEX activity_records ON records(activity);
CREATE TABLE locations(
 source_id TEXT PRIMARY KEY, history_id TEXT NOT NULL, kind TEXT NOT NULL,
 ordinal INTEGER NOT NULL, activity TEXT NOT NULL, record_json TEXT NOT NULL,
 receipt_json TEXT, portable_digest TEXT
);
CREATE INDEX history_locations ON locations(history_id,kind,ordinal DESC);
CREATE TABLE archive_heads(history_id TEXT PRIMARY KEY, portable_digest TEXT NOT NULL);
CREATE TABLE aliases(source_id TEXT NOT NULL, alias TEXT NOT NULL, kind TEXT NOT NULL,
 history_id TEXT NOT NULL, PRIMARY KEY(source_id,alias,kind,history_id));
CREATE INDEX history_aliases ON aliases(alias,kind,history_id);
CREATE TABLE sessions(
 chat TEXT NOT NULL, generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
 kind TEXT NOT NULL, stopped TEXT, activity TEXT NOT NULL, history_id TEXT,
 record_json TEXT NOT NULL,
 PRIMARY KEY(chat,generation)
);
CREATE INDEX session_recency ON sessions(kind,activity DESC);
CREATE INDEX session_history ON sessions(history_id,activity DESC);
CREATE TABLE work_chats(work TEXT NOT NULL, chat TEXT NOT NULL, PRIMARY KEY(work,chat));
CREATE TABLE cursors(source TEXT PRIMARY KEY, inode TEXT, extent INTEGER, tail TEXT);
"""


class HistoryIndexIncomplete(RuntimeError):
    """Discovery cannot safely promise complete candidate membership."""


@dataclass(frozen=True)
class IndexCoverage:
    generation: str
    build: str
    complete: bool
    pending: tuple[str, ...] = ()
    activity_provisional: tuple[str, ...] = ()


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


def _connect(path: Path, *, fresh: bool = False, timeout: float = 2) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        if not fresh:
            db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        db.close()
        raise
    return db


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
        db: sqlite3.Connection,
        source_id: str,
        history_id: str,
        state: SpawnRecord | None = None,
        session: SessionRecord | None = None,
    ) -> None:
        db.execute("DELETE FROM aliases WHERE source_id=?", (source_id,))
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
        db.executemany(
            "INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
            ((source_id, name, kind, history_id) for name, kind in names),
        )

    def _refresh(self, db: sqlite3.Connection, history_id: str) -> None:
        location = db.execute(
            "SELECT * FROM locations l WHERE history_id=? AND (kind='spawn' OR portable_digest="
            "(SELECT portable_digest FROM archive_heads h WHERE h.history_id=l.history_id)) "
            "ORDER BY (kind='spawn') DESC,ordinal DESC,source_id LIMIT 1",
            (history_id,),
        ).fetchone()
        db.execute("DELETE FROM records WHERE history_id=?", (history_id,))
        if location is None:
            return
        record = SpawnRecord.model_validate_json(location["record_json"])
        receipt = json.loads(location["receipt_json"]) if location["receipt_json"] else None
        active = record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES
        session = None
        if location["kind"] == "spawn":
            related = db.execute(
                "SELECT record_json FROM sessions WHERE history_id=? OR (chat=? AND generation=?) "
                "ORDER BY activity DESC LIMIT 1",
                (history_id, record.chat_id, record.session_instance_id),
            ).fetchone()
            if related:
                session = SessionRecord.model_validate_json(related[0])
        activity = last_activity(record, session, location["activity"])
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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

    def _spawn(self, db: sqlite3.Connection, key: str) -> bool:
        source_id = f"spawn:{key}"
        old = db.execute(
            "SELECT history_id FROM locations WHERE source_id=?", (source_id,)
        ).fetchone()
        db.execute("DELETE FROM locations WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM aliases WHERE source_id=?", (source_id,))
        if old:
            self._refresh(db, old[0])
        record = read_state(self.root / "spawns", key, include_prompt=False)
        if record is None:
            return False
        history_id = str(record.history_id or uuid5(NAMESPACE_URL, f"{self.root}:{key}"))
        if db.execute(
            "SELECT 1 FROM locations WHERE history_id=? AND kind='spawn'", (history_id,)
        ).fetchone():
            raise ValueError(f"Conflicting loose copies of history {history_id}")
        activity = transcript_activity(
            self.root / "spawns" / key / "history.jsonl",
            record.terminal.finished_at if record.terminal else record.started_at or "",
        )
        db.execute(
            "INSERT INTO locations VALUES (?,?, 'spawn',0,?,?,NULL,NULL)",
            (source_id, history_id, activity, record.model_dump_json()),
        )
        self._aliases(db, source_id, history_id, state=record)
        self._refresh(db, history_id)
        return record.record_mode != "historical" and record.status not in TERMINAL_SPAWN_STATUSES

    def _sessions(self, db: sqlite3.Connection) -> None:
        path = self.root / "sessions.jsonl"
        if not path.exists():
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM work_chats")
            db.execute("DELETE FROM aliases WHERE source_id LIKE 'session:%'")
            db.execute("DELETE FROM cursors WHERE source='sessions'")
            return
        stat = path.stat()
        inode = f"{stat.st_dev}:{stat.st_ino}"
        cursor = db.execute("SELECT * FROM cursors WHERE source='sessions'").fetchone()
        offset = 0
        if (
            cursor
            and cursor["inode"] == inode
            and cursor["extent"] <= stat.st_size
            and cursor["tail"] == _tail(path, cursor["extent"])
        ):
            offset = cursor["extent"]
        else:
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM work_chats")
            db.execute("DELETE FROM aliases WHERE source_id LIKE 'session:%'")
        with path.open("rb") as handle:
            handle.seek(offset)
            while line := handle.readline():
                if not line.endswith(b"\n"):
                    break
                end = handle.tell()
                try:
                    event = _parse_event(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    offset = end
                    continue
                if isinstance(event, SessionUpdateEvent) and event.active_work_id:
                    db.execute(
                        "INSERT OR IGNORE INTO work_chats VALUES (?,?)",
                        (event.active_work_id.strip(), event.chat_id),
                    )
                if event is not None:
                    generation = event.session_instance_id
                    if not generation:
                        if isinstance(event, (SessionStartEvent, SessionHistoricalEvent)):
                            generation = f"legacy:{offset}"
                        else:
                            latest = db.execute(
                                "SELECT generation FROM sessions WHERE chat=? "
                                "AND generation LIKE 'legacy:%' ORDER BY ordinal DESC LIMIT 1",
                                (event.chat_id,),
                            ).fetchone()
                            generation = latest[0] if latest else ""
                    found = db.execute(
                        "SELECT * FROM sessions WHERE chat=? AND generation=?",
                        (event.chat_id, generation),
                    ).fetchone()
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
                                "SELECT history_id FROM locations WHERE source_id=?",
                                (f"spawn:{record.spawn_id}",),
                            ).fetchone()
                            history_id = linked[0] if linked else None
                        if history_id is None and generation:
                            linked = db.execute(
                                "SELECT history_id FROM records WHERE chat=? "
                                "AND archive_id IS NULL "
                                "AND json_extract(record_json,'$.session_instance_id')=? LIMIT 1",
                                (record.chat_id, generation),
                            ).fetchone()
                            history_id = linked[0] if linked else None
                        ordinal = (
                            offset
                            if isinstance(event, (SessionStartEvent, SessionHistoricalEvent))
                            else (found["ordinal"] if found else offset)
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                            (
                                record.chat_id,
                                generation,
                                ordinal,
                                record.kind,
                                record.stopped_at,
                                canonical_time(record.stopped_at or record.started_at),
                                history_id,
                                record.model_dump_json(),
                            ),
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
            "INSERT OR REPLACE INTO cursors VALUES ('sessions',?,?,?)",
            (inode, offset, _tail(path, offset)),
        )

    def _project(self, db: sqlite3.Connection, source: HistorySource) -> bool:
        if source.kind == "spawn":
            return self._spawn(db, source.key)
        if source.kind == "sessions":
            self._sessions(db)
            return False
        self._catalog(db)
        return False

    def _catalog(self, db: sqlite3.Connection) -> None:
        from meridian.lib.state.retention_archive import catalog_heads, read_receipts

        receipts = read_receipts(self.root)
        db.execute("DELETE FROM archive_heads")
        db.executemany("INSERT INTO archive_heads VALUES (?,?)", catalog_heads(receipts).items())
        affected = {
            row[0] for row in db.execute("SELECT history_id FROM locations WHERE kind='archive'")
        }
        db.execute("DELETE FROM locations WHERE kind='archive'")
        db.execute("DELETE FROM aliases WHERE source_id LIKE 'archive:%'")
        for ordinal, receipt in enumerate(receipts):
            for record in receipt.records:
                history_id = str(record.history_id)
                source_id = f"archive:{receipt.archive_id}:{receipt.location_id}:{history_id}"
                db.execute(
                    "INSERT OR REPLACE INTO locations VALUES (?,?, 'archive',?,?,?,?,?)",
                    (
                        source_id,
                        history_id,
                        ordinal,
                        canonical_time(record.activity),
                        record.state.model_dump_json(),
                        receipt.model_copy(update={"records": (record,)}).model_dump_json(),
                        record.portable_digest,
                    ),
                )
                self._aliases(db, source_id, history_id, state=record.state, session=record.session)
                affected.add(history_id)
        for history_id in affected:
            self._refresh(db, history_id)

    def _drain(
        self,
        db: sqlite3.Connection,
        target: tuple[DirtySource, ...],
        deadline: float,
    ) -> tuple[list[DirtySource], list[str], list[str]]:
        changes = HistoryChanges(self.root)
        acknowledged: list[DirtySource] = []
        pending: list[str] = []
        active: list[str] = []
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
            except TimeoutError:
                pending.append(marker.source.name)
        db.commit()
        return acknowledged, pending, active

    def rebuild(self, *, reset: bool = False, timeout: float = 60) -> IndexCoverage:
        changes = HistoryChanges(self.root)
        deadline = time.monotonic() + timeout
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(
                changes.mutation_lock,
                mode="exclusive" if reset else "shared",
                timeout=_remaining(deadline),
            ),
        ):
            if reset:
                # Full quiescent scan replaces unknown coordination; locks are never removed.
                with lock_file(changes.marker_lock, timeout=_remaining(deadline)):
                    from meridian.lib.state.atomic import atomic_write_text

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
                db = _connect(stage, fresh=True, timeout=_remaining(deadline))
                build = str(uuid4())
                try:
                    db.executescript(_SCHEMA)
                    db.execute("INSERT INTO meta VALUES (2,?,?)", (generation, build))
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
                    acknowledged, pending, active = self._drain(db, target, deadline)
                    if pending:
                        raise HistoryIndexIncomplete("Rebuild timed out resolving changed sources")
                    db.execute("ANALYZE")
                    db.commit()
                finally:
                    db.close()
                # The root gate is already held: ordinary readers must never take it
                # while holding a database gate. Catchup/rebuild share catchup.lock.
                with lock_file(self.database_lock, timeout=_remaining(deadline)):
                    if self.path.exists():
                        try:
                            old = _connect(self.path, timeout=_remaining(deadline))
                            try:
                                busy, _, _ = old.execute(
                                    "PRAGMA wal_checkpoint(TRUNCATE)"
                                ).fetchone()
                                if busy:
                                    raise HistoryIndexIncomplete("Readers still own the old WAL")
                            finally:
                                old.close()
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
            finally:
                for suffix in ("", "-journal"):
                    Path(str(stage) + suffix).unlink(missing_ok=True)
            for marker in acknowledged:
                changes.acknowledge(marker)
            return IndexCoverage(generation, build, True, activity_provisional=tuple(active))

    def catch_up(self, *, timeout: float = 2) -> IndexCoverage:
        if not self.path.exists():
            return self.rebuild(timeout=timeout)
        changes = HistoryChanges(self.root)
        deadline = time.monotonic() + timeout
        with (
            lock_file(self.catchup_lock, timeout=_remaining(deadline)),
            lock_file(changes.mutation_lock, mode="shared", timeout=_remaining(deadline)),
            lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)),
        ):
            generation, target = changes.capture(timeout=_remaining(deadline))
            db = _connect(self.path, timeout=_remaining(deadline))
            try:
                meta = db.execute("SELECT * FROM meta").fetchone()
                if meta is None or meta["generation"] != generation:
                    raise HistoryCoordinationError(
                        "History baseline generation mismatch; rebuild required"
                    )
                if meta["version"] == 2:
                    acknowledged, pending, active = self._drain(db, target, deadline)
                    for marker in acknowledged:
                        changes.acknowledge(marker)
                    return IndexCoverage(
                        generation, meta["build"], not pending, tuple(pending), tuple(active)
                    )
            finally:
                db.close()
        # Schema changes replace only the disposable projection through normal rebuild.
        return self.rebuild(timeout=_remaining(deadline))

    @contextmanager
    def query(self, *, deadline: float | None = None) -> Generator[sqlite3.Connection]:
        deadline = time.monotonic() + 2 if deadline is None else deadline
        coverage = self.catch_up(timeout=_remaining(deadline))
        if not coverage.complete:
            raise HistoryIndexIncomplete(
                f"History index has unresolved sources: {coverage.pending}"
            )
        with lock_file(self.database_lock, mode="shared", timeout=_remaining(deadline)):
            db = _connect(self.path, timeout=_remaining(deadline))
            try:
                yield db
            finally:
                db.close()

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
        for record in self.spawns():
            history_id = str(record.history_id)
            if history_id not in seen:
                references.append((history_id, history_id, record.session_instance_id or ""))
        return tuple(references)

    def preview_count(self) -> int:
        with self.query() as db:
            return sum(
                self._preview_generation_matches(db, row[0])
                and self._preview_binding_matches(db, row[1], row[2])
                for row in db.execute(
                    "SELECT key,history_id,archive_digest FROM previews"
                ).fetchall()
            )

    def preview_cache(self, key: str) -> tuple[str, str | None] | None:
        """Bounded cache-only lookup: never catch up metadata or open source content."""
        if not self.path.exists():
            return None
        with lock_file(self.database_lock, mode="shared", timeout=0.005):
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.005)
            try:
                meta = db.execute("SELECT version,build FROM meta").fetchone()
                if meta is None or meta[0] != 2:
                    return None
                row = db.execute(
                    "SELECT history_id,archive_digest,value FROM previews WHERE key=?", (key,)
                ).fetchone()
                if (
                    row is not None
                    and self._preview_generation_matches(db, key)
                    and self._preview_binding_matches(db, row[0], row[1])
                ):
                    return meta[1], row[2]
                return meta[1], None
            finally:
                db.close()

    @staticmethod
    def _preview_generation_matches(db: sqlite3.Connection, key: str) -> bool:
        history_id, generation, ref = json.loads(key)
        if history_id is not None:
            return True  # The portable UUID, not a reusable alias, resolves this source.
        if not generation:
            return False
        row = db.execute(
            "SELECT record_json FROM sessions WHERE chat=? ORDER BY ordinal DESC LIMIT 1", (ref,)
        ).fetchone()
        if row is None:
            return False
        session = SessionRecord.model_validate_json(row[0])
        return generation == (
            session.session_instance_id or session.harness_session_id or session.started_at
        )

    @staticmethod
    def _preview_binding_matches(
        db: sqlite3.Connection, history_id: str | None, archive_digest: str | None
    ) -> bool:
        if history_id is None:
            return archive_digest is None
        row = db.execute(
            "SELECT r.archive_id,h.portable_digest FROM records r "
            "LEFT JOIN archive_heads h ON h.history_id=r.history_id WHERE r.history_id=?",
            (history_id,),
        ).fetchone()
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
                "SELECT portable_digest FROM archive_heads WHERE history_id=?", (history_id,)
            ).fetchone()
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
        ):
            db = _connect(self.path, timeout=0.1)
            try:
                db.execute("BEGIN IMMEDIATE")
                meta = db.execute("SELECT build FROM meta").fetchone()
                if (
                    meta is None
                    or meta[0] != build
                    or not self._preview_generation_matches(db, key)
                    or not self._preview_binding_matches(db, history_id, archive_digest)
                ):
                    return False
                old = db.execute(
                    "SELECT history_id,archive_digest,value FROM previews WHERE key=?", (key,)
                ).fetchone()
                if (old[2] if old else None) != previous and (
                    old is None or self._preview_binding_matches(db, old[0], old[1])
                ):
                    return False
                value = prepare_value()
                if value is None:
                    return False
                if len(value.encode("utf-8")) > 64 * 1024:
                    raise ValueError("Preview exceeds the bounded cache contract")
                db.execute(
                    "INSERT OR REPLACE INTO previews VALUES (?,?,?,?)",
                    (key, history_id, archive_digest, value),
                )
                db.commit()
                return True
            finally:
                db.close()

    def read_targets(
        self, ref: str, *, destination: Path | None = None, deadline: float | None = None
    ) -> tuple[HistoryReadTarget, ...]:
        from meridian.lib.state.retention_archive import ArchiveReceipt, archive_locations

        deadline = time.monotonic() + 2 if deadline is None else deadline
        with self.query(deadline=deadline) as db:
            direct = db.execute(
                "SELECT history_id FROM records WHERE history_id=?", (ref,)
            ).fetchone()
            if direct:
                history_id = direct[0]
            else:
                kind = (
                    "spawn"
                    if ref.startswith("p") and ref[1:].isdigit()
                    else ("chat" if ref.startswith("c") and ref[1:].isdigit() else "harness")
                )
                matches = db.execute(
                    "SELECT r.history_id,MAX(a.source_id NOT LIKE 'archive:%') AS local "
                    "FROM aliases a JOIN records r USING(history_id) WHERE a.alias=? AND a.kind=? "
                    "GROUP BY r.history_id ORDER BY local DESC,"
                    "(r.kind='primary') DESC,r.started DESC",
                    (ref, kind),
                ).fetchall()
                if not matches:
                    return ()
                if len(matches) > 1 and not matches[0]["local"]:
                    raise ValueError("Ambiguous archive origin alias; use a portable history UUID")
                history_id = matches[0]["history_id"]
            locations = db.execute(
                "SELECT l.* FROM locations l JOIN records r ON r.history_id=l.history_id "
                "WHERE l.history_id=? AND (l.kind='spawn' OR (r.archive_id IS NOT NULL "
                "AND l.portable_digest=(SELECT portable_digest FROM archive_heads "
                "WHERE history_id=l.history_id))) ORDER BY (l.kind='spawn') DESC,l.ordinal DESC",
                (history_id,),
            ).fetchall()
        receipts: list[ArchiveReceipt] = []
        for location in locations:
            state = SpawnRecord.model_validate_json(location["record_json"])
            if location["kind"] == "spawn":
                path = self.root / "spawns" / state.id / "history.jsonl"
                if path.is_file():
                    from meridian.lib.state.history_codec import TranscriptHeader

                    with path.open("rb") as handle:
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
                    return (HistoryReadTarget(state, path),)
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
            rows = db.execute(
                "SELECT l.*,h.portable_digest AS current_digest,r.archive_id "
                "FROM locations l LEFT JOIN archive_heads h USING(history_id) "
                "LEFT JOIN records r USING(history_id) WHERE l.kind='archive' "
                "ORDER BY l.history_id,l.ordinal DESC"
            ).fetchall()
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
            rows = db.execute(
                "SELECT history_id,local_id,chat,archive_id,activity FROM records "
                + ("" if include_archives else "WHERE archive_id IS NULL ")
                + "UNION ALL SELECT s.chat,s.chat,s.chat,NULL,s.activity FROM sessions s "
                "WHERE NOT EXISTS (SELECT 1 FROM records r WHERE r.chat=s.chat) "
                "AND NOT EXISTS (SELECT 1 FROM sessions newer WHERE newer.chat=s.chat "
                "AND newer.ordinal>s.ordinal) ORDER BY activity DESC,history_id"
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
            "chat_id": "chat",
            "owner_chat_id": "owner",
            "parent_id": "parent",
            "work_id": "work",
            "status": "status",
            "spawn_id": "local_id",
        }
        conditions = ["archive_id IS NULL"]
        values: list[str] = []
        if related_chat_ids is not None:
            placeholders = ",".join("?" for _ in related_chat_ids)
            conditions.append(f"(chat IN ({placeholders}) OR owner IN ({placeholders}))")
            values.extend(sorted(related_chat_ids) * 2)
        for key, value in filters.items():
            if key not in columns:
                raise ValueError(f"Unknown history filter: {key}")
            if isinstance(value, set):
                conditions.append(f"{columns[key]} IN (" + ",".join("?" for _ in value) + ")")
                values.extend(sorted(value))
            elif value is not None:
                conditions.append(f"{columns[key]}=?")
                values.append(value)
        with self.query() as db:
            return tuple(
                SpawnRecord.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT record_json FROM records WHERE "
                    + " AND ".join(conditions)
                    + (
                        " ORDER BY activity,history_id"
                        if oldest_first
                        else " ORDER BY started DESC,local_id DESC"
                    ),
                    values,
                )
            )

    def work_chat_ids(self, work_id: str, *, deadline: float | None = None) -> set[str]:
        with self.query(deadline=deadline) as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT chat FROM work_chats WHERE work=? "
                    "UNION SELECT chat FROM records WHERE work=?",
                    (work_id, work_id),
                )
                if row[0]
            }

    def recent_sessions(
        self, *, limit: int, live_chat_ids: set[str]
    ) -> tuple[list[SessionRecord | SpawnRecord], int]:
        placeholders = ",".join("?" for _ in live_chat_ids) or "NULL"
        corpus = f"""WITH browse AS (
            SELECT s.record_json,'session' AS source,s.activity,
            (s.chat IN ({placeholders})) AS live,CAST(substr(s.chat,2) AS INTEGER) AS tie
            FROM sessions s WHERE kind='primary' AND NOT EXISTS
            (SELECT 1 FROM sessions newer WHERE newer.chat=s.chat AND newer.ordinal>s.ordinal)
            AND NOT EXISTS (SELECT 1 FROM records r WHERE r.archive_id IS NOT NULL
                            AND r.history_id=s.history_id)
            UNION ALL SELECT record_json,'archive',activity,0,0 FROM records
            WHERE archive_id IS NOT NULL AND kind='primary'
        ) """
        values = tuple(sorted(live_chat_ids))
        with self.query() as db:
            total = db.execute(corpus + "SELECT COUNT(*) FROM browse", values).fetchone()[0]
            rows = db.execute(
                corpus + "SELECT record_json,source FROM browse "
                "ORDER BY live DESC,activity DESC,tie DESC LIMIT ?",
                (*values, limit),
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
        sql = """SELECT s.record_json FROM sessions s WHERE NOT EXISTS
        (SELECT 1 FROM sessions newer WHERE newer.chat=s.chat AND newer.ordinal>s.ordinal)
        """
        values: tuple[Any, ...] = ()
        if chat_ids is not None:
            if not chat_ids:
                return []
            sql += " AND s.chat IN (" + ",".join("?" for _ in chat_ids) + ")"
            values = tuple(sorted(chat_ids))
        sql += " ORDER BY s.ordinal DESC"
        if limit is not None:
            if limit <= 0:
                raise ValueError("Session limit must be positive")
            sql += " LIMIT ?"
            values += (limit,)
        with self.query() as db:
            return [SessionRecord.model_validate_json(row[0]) for row in db.execute(sql, values)]


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
