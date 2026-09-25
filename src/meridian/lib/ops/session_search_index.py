"""Refresh disposable search rows from authoritative bindings and exact native sources."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.native_witness import FileWitness, Witness, file_witness
from meridian.lib.harness.opencode_search_source import opencode_session_witnesses
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
from meridian.lib.ops.session_transcript import (
    ParsedSessionTranscript,
    SessionLogRoute,
    TranscriptBudget,
    parse_session_target,
    read_native_source,
)
from meridian.lib.state.native_search_index import (
    PARSER_VERSION,
    NativeSearchIndex,
    NativeSearchUnavailable,
    SearchRow,
    SourceRecord,
    TranscriptEntry,
    discard_native_search_index,
    native_search_index_path,
    search_text,
)
from meridian.lib.state.session_fold import by_native_key
from meridian.lib.state.session_store import list_all_session_records

LAZY_SOURCE_BYTES = 64 * 1024 * 1024


def native_bindings(runtime_root: Path) -> dict[NativeKey, tuple[str, ...]]:
    """Invert authoritative bindings, preserving newest-first chat aliases."""
    records = sorted(
        list_all_session_records(runtime_root), key=lambda record: record.started_at, reverse=True
    )
    return {
        key: tuple(record.chat_id for record in aliases)
        for key, aliases in by_native_key({record.chat_id: record for record in records}).items()
    }


@dataclass(frozen=True)
class NativeSource:
    locator: Path
    witness: Witness

    @property
    def activity(self) -> int:
        return self.witness.activity_ns


class SearchStatus(TypedDict, total=False):
    search_fresh: int
    search_stale: int
    search_unindexed: int
    search_bytes: int
    search_unavailable: int


@dataclass
class SearchProjection:
    runtime_root: Path
    project_root: Path
    bindings: dict[NativeKey, tuple[str, ...]]
    index: NativeSearchIndex | None
    stored: dict[NativeKey, SourceRecord]
    cold: bool = False
    sources: dict[NativeKey, NativeSource] = field(default_factory=dict[NativeKey, NativeSource])
    fresh: set[NativeKey] = field(default_factory=set[NativeKey])
    errors: dict[NativeKey, str] = field(default_factory=dict[NativeKey, str])
    warnings: dict[NativeKey, str] = field(default_factory=dict[NativeKey, str])
    parsed: dict[NativeKey, ParsedSessionTranscript] = field(
        default_factory=dict[NativeKey, ParsedSessionTranscript]
    )

    @classmethod
    def open(cls, runtime_root: Path, project_root: Path) -> SearchProjection:
        bindings = native_bindings(runtime_root)
        path = native_search_index_path(runtime_root)
        cold = not path.exists()
        try:
            try:
                index = NativeSearchIndex.for_runtime(runtime_root, timeout=0)
                stored = index.inventory()
                cold = cold or (
                    bool(stored)
                    and all(row.parser_version != PARSER_VERSION for row in stored.values())
                )
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                    raise
                # No authoritative bytes live here. Recreate a damaged projection.
                discard_native_search_index(runtime_root)
                index = NativeSearchIndex.for_runtime(runtime_root, timeout=0)
                stored = {}
                cold = True
        except NativeSearchUnavailable:
            index, stored, cold = None, {}, False
        return cls(runtime_root, project_root, bindings, index, stored, cold=cold)

    def scope(self, chat_filter: frozenset[str] | None) -> dict[NativeKey, tuple[str, ...]]:
        if chat_filter is None:
            return self.bindings
        return {
            key: selected
            for key, chats in self.bindings.items()
            if (selected := tuple(chat for chat in chats if chat in chat_filter))
        }

    def inspect(self, keys: dict[NativeKey, tuple[str, ...]], *, deadline: float) -> None:
        """Stat cached locators and query each OpenCode DB once; never trust a cached witness."""
        databases: dict[str, list[NativeKey]] = defaultdict(list)
        for key in keys:
            if key.harness == "opencode":
                databases[key.native_store].append(key)
            elif (stored := self.stored.get(key)) is not None:
                # Refresh resolves absent locators through the exact adapter.
                with suppress(OSError):
                    self.sources[key] = NativeSource(
                        Path(stored.locator), file_witness(Path(stored.locator))
                    )
        for database, members in databases.items():
            if time.monotonic() >= deadline:
                break
            try:
                witnesses = opencode_session_witnesses(
                    Path(database), (key.session_id for key in members)
                )
                for key in members:
                    if (witness := witnesses.get(key.session_id)) is not None:
                        self.sources[key] = NativeSource(Path(database), witness)
                    else:
                        self.errors[key] = "missing"
            except (OSError, ValueError, sqlite3.Error) as exc:
                for key in members:
                    self.errors[key] = str(exc)
        for key, source in self.sources.items():
            stored = self.stored.get(key)
            if stored and stored.is_current(source.witness.encode()):
                if stored.status == "complete":
                    self.fresh.add(key)
                if stored.reasons or stored.status != "complete":
                    (self.warnings if key in self.fresh else self.errors)[key] = (
                        "; ".join(stored.reasons) or stored.status
                    )

    def refresh(
        self, keys: dict[NativeKey, tuple[str, ...]], *, deadline: float, rebuild: bool = False
    ) -> None:
        with self.index.write_batch() if self.index else nullcontext():
            if self.index:
                self.index.timeout = min(2, max(0, deadline - time.monotonic()))
                removed = self.stored.keys() - self.bindings.keys()
                removed |= {key for key, error in self.errors.items() if error == "missing"}
                for key in removed:
                    if time.monotonic() >= deadline:
                        break
                    self.index.remove_source(key)
            pending = [key for key in keys if key not in self.fresh and key not in self.errors]
            # Until a locator exists, the authoritative chat order is the only
            # available recency signal. Warm refresh uses exact native activity.
            if not self.cold:
                pending.sort(
                    key=lambda key: (
                        self.sources[key].activity if key in self.sources else 2**63 - 1
                    ),
                    reverse=True,
                )
            for key in pending:
                if time.monotonic() >= deadline:
                    break
                try:
                    source = self.sources.get(key)
                    if source is None:
                        adapter = get_default_harness_registry().get_subprocess_harness(
                            HarnessId(key.harness)
                        )
                        locator = adapter.resolve_native_session_file(
                            session_id=key.session_id, native_store=Path(key.native_store)
                        )
                        if locator is None:
                            self.errors[key] = "missing"
                            if self.index:
                                self.index.remove_source(key)
                            continue
                        source = NativeSource(locator, file_witness(locator))
                        self.sources[key] = source
                    transcript = self._read(key, source, deadline=deadline, rebuild=rebuild)
                    if transcript is None:
                        continue
                    source = self.sources[key]
                    reasons = transcript.read_reasons
                    complete = transcript.search_ready
                    if self.index:
                        self.index.timeout = min(2, max(0, deadline - time.monotonic()))
                        self.index.replace_source(
                            key,
                            locator=source.locator,
                            witness=source.witness.encode(),
                            activity=source.activity,
                            entries=(
                                TranscriptEntry(
                                    e.ordinal,
                                    " ".join(e.content.split()),
                                    e.segment_index,
                                    e.start_segment_message_index,
                                    e.end_segment_message_index,
                                    e.role,
                                    e.kind,
                                    e.is_placeholder,
                                )
                                for e in transcript.all_entries
                            ),
                            status="complete" if complete else "partial",
                            reasons=reasons,
                        )
                    else:
                        self.parsed[key] = transcript
                    if complete:
                        self.fresh.add(key)
                    if reasons or not complete:
                        (self.warnings if complete else self.errors)[key] = (
                            "; ".join(reasons) or "partial"
                        )
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.errors[key] = str(exc)

    def _read(
        self, key: NativeKey, source: NativeSource, *, deadline: float, rebuild: bool
    ) -> ParsedSessionTranscript | None:
        for _attempt in range(2):
            if time.monotonic() >= deadline:
                break
            # inspect's witness orders work; this read takes its own immediate stat.
            before = (
                file_witness(source.locator) if isinstance(source.witness, FileWitness) else None
            )
            if before and not rebuild and before.size > LAZY_SOURCE_BYTES:
                self.errors[key] = "over lazy source cap — run meridian session index rebuild"
                return None
            budget = None if rebuild else TranscriptBudget(deadline, LAZY_SOURCE_BYTES)
            target = SessionLogTarget(TranscriptSource.native(key, source.locator))
            read = read_native_source(target.source, budget=budget)
            transcript = parse_session_target(
                project_root=self.project_root,
                runtime_root=self.runtime_root,
                target=target,
                route=SessionLogRoute("ref", self.bindings[key][0]),
                native_read=read,
            )
            if budget and budget.exhausted:
                return None
            if (
                isinstance(read.witness, FileWitness)
                and file_witness(source.locator) != read.witness
            ):
                self.errors[key] = "changed during refresh"
                continue
            assert read.witness is not None
            self.sources[key] = NativeSource(source.locator, read.witness)
            self.errors.pop(key, None)
            return transcript
        return None

    def rebuild(self) -> SearchStatus:
        if self.index:
            self.index.rebuild()
        self.stored.clear()
        self.sources.clear()
        self.fresh.clear()
        self.errors.clear()
        self.warnings.clear()
        self.parsed.clear()
        self.inspect(self.bindings, deadline=float("inf"))
        self.refresh(self.bindings, deadline=float("inf"), rebuild=True)
        if self.index:
            self.stored = self.index.inventory()
        return self.status()

    @classmethod
    def read_status(
        cls, runtime_root: Path, project_root: Path, *, deadline: float
    ) -> SearchStatus:
        if not native_search_index_path(runtime_root).exists():
            return SearchStatus(search_fresh=0, search_unindexed=len(native_bindings(runtime_root)))
        projection = cls.open(runtime_root, project_root)
        projection.inspect(projection.bindings, deadline=deadline)
        return projection.status()

    def status(self) -> SearchStatus:
        indexed = self.stored.keys() & self.bindings.keys()
        current = {
            key
            for key in indexed
            if key in self.sources
            and self.stored[key].is_current(self.sources[key].witness.encode())
        }
        return SearchStatus(
            search_fresh=len(current),
            search_stale=len(indexed - current),
            search_unavailable=len(self.errors),
            search_unindexed=len(self.bindings.keys() - indexed),
            search_bytes=self.index.path.stat().st_size if self.index else 0,
        )

    def search(
        self, query: str, *, limit: int | None = 101, deadline: float | None = None
    ) -> list[SearchRow]:
        if self.index:
            return self.index.search(query, keys=self.fresh, limit=limit, deadline=deadline)
        rows = [
            SearchRow(
                key,
                str(self.sources[key].locator),
                self.sources[key].activity,
                e.ordinal,
                e.segment_index,
                e.start_segment_message_index,
                e.end_segment_message_index,
                e.role,
                e.kind,
                " ".join(e.content.split()),
            )
            for key, transcript in self.parsed.items()
            if key in self.fresh
            for e in transcript.all_entries
            if not e.is_placeholder and query.lower() in search_text(e.content)
        ]
        rows.sort(key=lambda row: (-row.activity, row.segment or 0, row.ordinal))
        return rows[:limit] if limit is not None else rows
