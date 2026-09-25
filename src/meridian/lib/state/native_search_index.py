"""Disposable, native-keyed FTS projection for transcript search.

This module stores parsed display entries only. It neither resolves native
sources nor owns chat bindings; callers must validate source freshness before
using results and re-check matches against the stored display text.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.core.native_identity import NativeKey

SCHEMA_VERSION = 1
PARSER_VERSION = 2
INDEX_FILENAME = "native-search-v1.sqlite3"
MINIMUM_SQLITE_VERSION = (3, 43, 0)


class NativeSearchUnavailable(RuntimeError):
    """The local SQLite build cannot create the contentless FTS projection."""


def discard_native_search_index(runtime_root: Path) -> None:
    """Delete only the disposable projection and its SQLite sidecars."""
    path = runtime_root / "history-index" / INDEX_FILENAME
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def sqlite_search_supported() -> bool:
    """Whether SQLite supports contentless FTS deletion (introduced in 3.43)."""
    return sqlite3.sqlite_version_info >= MINIMUM_SQLITE_VERSION


@dataclass(frozen=True)
class TranscriptEntry:
    ordinal: int
    content: str
    segment: int | None = None
    seg_start: int | None = None
    seg_end: int | None = None
    role: str | None = None
    kind: str | None = None
    is_placeholder: bool = False


@dataclass(frozen=True)
class SearchRow:
    key: NativeKey
    locator: str
    activity: int
    ordinal: int
    segment: int | None
    seg_start: int | None
    seg_end: int | None
    role: str | None
    kind: str | None
    content: str


@dataclass(frozen=True)
class SourceRecord:
    locator: str
    witness: str
    parser_version: int
    activity: int
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FileWitness:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class OpenCodeV1Witness:
    part_count: int
    part_updated_ms: int | None
    message_count: int
    message_updated_ms: int | None
    session_updated_ms: int


@dataclass(frozen=True)
class OpenCodeV2Witness:
    message_count: int
    max_seq: int | None
    message_updated_ms: int | None
    session_updated_ms: int


Witness = FileWitness | OpenCodeV1Witness | OpenCodeV2Witness
SearchMode = Literal["fts", "scan"]


def file_witness(path: Path) -> FileWitness:
    """Return the exact stat tuple used for file-backed freshness."""
    stat = path.stat()
    return FileWitness(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def witness_json(witness: Witness) -> str:
    """Serialize a typed witness deterministically for equality checks."""
    if isinstance(witness, FileWitness):
        family = "file"
        values = (witness.device, witness.inode, witness.size, witness.mtime_ns)
    elif isinstance(witness, OpenCodeV1Witness):
        family = "opencode-v1"
        values = (
            witness.part_count,
            witness.part_updated_ms,
            witness.message_count,
            witness.message_updated_ms,
            witness.session_updated_ms,
        )
    else:
        family = "opencode-v2"
        values = (
            witness.message_count,
            witness.max_seq,
            witness.message_updated_ms,
            witness.session_updated_ms,
        )
    return json.dumps((family, values), separators=(",", ":"))


def normalize_index_text(content: str) -> str:
    """Python-folded, whitespace-normalized trigram input; never persisted."""
    return " ".join(content.split()).lower().replace("\0", " ")


def search_text(content: str) -> str:
    """The user-visible substring predicate shared with native search."""
    return " ".join(content.split()).lower()


def _query_match(query: str) -> str | None:
    folded = query.lower()
    if "\0" in folded or len(folded) < 3:
        return None
    grams = dict.fromkeys(folded[i : i + 3] for i in range(len(folded) - 2))
    # FTS5 treats quoted trigram strings literally, including punctuation.
    return " AND ".join('"' + gram.replace('"', '""') + '"' for gram in grams)


class NativeSearchIndex:
    """SQLite projection; each source replacement is one crash-safe transaction."""

    def __init__(self, path: Path, *, timeout: float = 2.0) -> None:
        if not sqlite_search_supported():
            raise NativeSearchUnavailable(
                f"SQLite {sqlite3.sqlite_version} is older than 3.43; "
                "native search index unavailable"
            )
        self.path = path
        self.timeout = timeout
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def for_runtime(cls, runtime_root: Path, *, timeout: float = 2.0) -> NativeSearchIndex:
        return cls(runtime_root / "history-index" / INDEX_FILENAME, timeout=timeout)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout)
        db.execute("PRAGMA foreign_keys = ON")
        # This WAL is a disposable projection. NORMAL keeps transactions atomic
        # without forcing durable-media synchronization for every indexed source.
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute(f"PRAGMA busy_timeout = {max(0, int(self.timeout * 1000))}")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
                return
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                  source_id INTEGER PRIMARY KEY,
                  harness TEXT NOT NULL, native_store TEXT NOT NULL, session_id TEXT NOT NULL,
                  locator TEXT NOT NULL, witness TEXT NOT NULL, parser_version INTEGER NOT NULL,
                  activity INTEGER NOT NULL, status TEXT NOT NULL, reasons TEXT,
                  UNIQUE(harness, native_store, session_id));
                CREATE TABLE IF NOT EXISTS entries(
                  rowid INTEGER PRIMARY KEY,
                  source_id INTEGER NOT NULL REFERENCES sources ON DELETE CASCADE,
                  ordinal INTEGER NOT NULL, segment INTEGER, seg_start INTEGER, seg_end INTEGER,
                  role TEXT, kind TEXT, is_placeholder INTEGER NOT NULL, content TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entries_source ON entries(source_id, ordinal);
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                  t, content='', contentless_delete=1, tokenize='trigram', detail='none');
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def write_batch(self) -> Generator[None]:
        """Keep WAL attached across per-source commits, without holding a read transaction.

        Closing the final connection checkpoints the WAL. Doing that per source
        repeatedly rewrites the growing FTS index; one refresh needs one lifetime.
        """
        with closing(self._connect()) as keeper:
            keeper.execute("SELECT value FROM meta").fetchall()
            yield

    def inventory(self) -> dict[NativeKey, SourceRecord]:
        """Bulk metadata only; callers supply authoritative keys and current witnesses."""
        with closing(self._connect()) as db:
            return {
                NativeKey(row[0], row[1], row[2]): SourceRecord(
                    row[3], row[4], row[5], row[6], row[7], tuple(json.loads(row[8] or "[]"))
                )
                for row in db.execute(
                    "SELECT harness,native_store,session_id,locator,witness,parser_version,"
                    "activity,status,reasons FROM sources"
                )
            }

    def get_witness(self, key: NativeKey) -> tuple[str, int] | None:
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT witness,parser_version FROM sources "
                "WHERE harness=? AND native_store=? AND session_id=?",
                (key.harness, key.native_store, key.session_id),
            ).fetchone()
        return (str(row[0]), int(row[1])) if row else None

    def is_fresh(
        self, key: NativeKey, witness: Witness, parser_version: int = PARSER_VERSION
    ) -> bool:
        stored = self.get_witness(key)
        return stored == (witness_json(witness), parser_version)

    def replace_source(
        self,
        key: NativeKey,
        *,
        locator: Path | str,
        witness: Witness,
        activity: int,
        entries: Iterable[TranscriptEntry],
        status: str = "complete",
        reasons: Iterable[str] = (),
        parser_version: int = PARSER_VERSION,
    ) -> None:
        """Atomically replace a source and its FTS rows; intended to be replayable."""
        witness_value = witness_json(witness)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT source_id,witness,parser_version FROM sources "
                "WHERE harness=? AND native_store=? AND session_id=?",
                (key.harness, key.native_store, key.session_id),
            ).fetchone()
            if source and source[1:] == (witness_value, parser_version):
                return
            if source:
                source_id = int(source[0])
                rowids = [
                    r[0]
                    for r in db.execute("SELECT rowid FROM entries WHERE source_id=?", (source_id,))
                ]
                if rowids:
                    db.executemany(
                        "DELETE FROM entries_fts WHERE rowid=?",
                        ((rowid,) for rowid in rowids),
                    )
                db.execute("DELETE FROM entries WHERE source_id=?", (source_id,))
                db.execute(
                    "UPDATE sources SET locator=?,witness=?,parser_version=?,activity=?,"
                    "status=?,reasons=? "
                    "WHERE source_id=?",
                    (
                        str(locator),
                        witness_value,
                        parser_version,
                        activity,
                        status,
                        json.dumps(list(reasons)),
                        source_id,
                    ),
                )
            else:
                cursor = db.execute(
                    "INSERT INTO sources(harness,native_store,session_id,locator,witness,"
                    "parser_version,activity,status,reasons) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        key.harness,
                        key.native_store,
                        key.session_id,
                        str(locator),
                        witness_value,
                        parser_version,
                        activity,
                        status,
                        json.dumps(list(reasons)),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an inserted source ID")
                source_id = cursor.lastrowid
            for entry in entries:
                cursor = db.execute(
                    "INSERT INTO entries(source_id,ordinal,segment,seg_start,seg_end,role,kind,"
                    "is_placeholder,content) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        entry.ordinal,
                        entry.segment,
                        entry.seg_start,
                        entry.seg_end,
                        entry.role,
                        entry.kind,
                        int(entry.is_placeholder),
                        entry.content,
                    ),
                )
                db.execute(
                    "INSERT INTO entries_fts(rowid,t) VALUES(?,?)",
                    (cursor.lastrowid, normalize_index_text(entry.content)),
                )
            db.commit()

    def remove_source(self, key: NativeKey) -> None:
        """Drop all projection rows for a no-longer-bound or missing key."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source_id FROM sources WHERE harness=? AND native_store=? AND session_id=?",
                (key.harness, key.native_store, key.session_id),
            ).fetchone()
            if row:
                source_id = int(row[0])
                ids = [
                    r[0]
                    for r in db.execute("SELECT rowid FROM entries WHERE source_id=?", (source_id,))
                ]
                if ids:
                    db.executemany("DELETE FROM entries_fts WHERE rowid=?", ((rid,) for rid in ids))
                db.execute("DELETE FROM sources WHERE source_id=?", (source_id,))

    def search(
        self,
        query: str,
        *,
        keys: Iterable[NativeKey] | None = None,
        limit: int | None = None,
        deadline: float | None = None,
    ) -> tuple[SearchMode, list[SearchRow]]:
        """Return exact substring hits; trigram FTS only nominates candidates."""
        match = _query_match(query)
        key_list = list(keys) if keys is not None else None
        where: list[str] = ["s.status='complete'", "e.is_placeholder=0"]
        params: list[object] = []
        if key_list is not None:
            if not key_list:
                return ("fts" if match is not None else "scan", [])
            where.append(
                "s.source_id IN (SELECT s2.source_id FROM json_each(?) k JOIN sources s2 "
                "ON s2.harness=json_extract(k.value,'$[0]') "
                "AND s2.native_store=json_extract(k.value,'$[1]') "
                "AND s2.session_id=json_extract(k.value,'$[2]'))"
            )
            params.append(
                json.dumps([(key.harness, key.native_store, key.session_id) for key in key_list])
            )
        join = "JOIN entries_fts f ON f.rowid=e.rowid " if match is not None else ""
        if match is not None:
            where.append("entries_fts MATCH ?")
            params.append(match)
        sql = (
            "SELECT s.harness,s.native_store,s.session_id,s.locator,s.activity,e.ordinal,e.segment,"
            "e.seg_start,e.seg_end,e.role,e.kind,e.rowid "
            "FROM entries e JOIN sources s ON s.source_id=e.source_id "
            + join
            + "WHERE "
            + " AND ".join(where)
            + " ORDER BY s.activity DESC,s.source_id,e.segment,e.ordinal"
        )
        hits: list[SearchRow] = []
        needle = query.lower()
        with closing(self._connect()) as db, db:
            if deadline is not None:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            for row in db.execute(sql, params):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Native search query deadline exceeded")
                # Sort/navigate only small candidate metadata. Fetch large display
                # text after nomination, stopping as soon as the verified limit is met.
                content = str(
                    db.execute("SELECT content FROM entries WHERE rowid=?", (row[11],)).fetchone()[
                        0
                    ]
                )
                if needle not in search_text(content):
                    continue
                hits.append(
                    SearchRow(
                        NativeKey(str(row[0]), str(row[1]), str(row[2])),
                        str(row[3]),
                        int(row[4] or 0),
                        int(row[5] or 0),
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        content,
                    )
                )
                if limit is not None and len(hits) >= limit:
                    break
        return ("fts" if match is not None else "scan", hits)

    def counts(self) -> tuple[int, int]:
        with closing(self._connect()) as db, db:
            return (
                int(db.execute("SELECT count(*) FROM sources").fetchone()[0]),
                int(db.execute("SELECT count(*) FROM entries").fetchone()[0]),
            )

    def rebuild(self) -> None:
        """Clear this disposable projection; orchestration repopulates from native sources."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM entries_fts")
            db.execute("DELETE FROM entries")
            db.execute("DELETE FROM sources")
            db.commit()
