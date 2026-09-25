"""Refresh disposable search rows from authoritative bindings and exact native sources."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.opencode_transcript import (
    opencode_session_witnesses,
    read_opencode_search_source,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
from meridian.lib.ops.session_transcript import (
    ParsedSessionTranscript,
    SessionLogRoute,
    TranscriptBudget,
    parse_session_target,
)
from meridian.lib.state.native_search_index import (
    INDEX_FILENAME,
    PARSER_VERSION,
    FileWitness,
    NativeSearchIndex,
    NativeSearchUnavailable,
    SearchRow,
    SourceRecord,
    TranscriptEntry,
    Witness,
    discard_native_search_index,
    file_witness,
    search_text,
    witness_json,
)
from meridian.lib.state.session_store import list_all_session_records

LAZY_SOURCE_BYTES = 64 * 1024 * 1024


def native_bindings(runtime_root: Path) -> dict[NativeKey, tuple[str, ...]]:
    """Only this seam folds authority; replace with session_fold.by_native_key when available."""
    grouped: dict[NativeKey, list[str]] = defaultdict(list)
    for record in sorted(
        list_all_session_records(runtime_root), key=lambda record: record.started_at, reverse=True
    ):
        key = record.native_key()
        if key is not None:
            grouped[key].append(record.chat_id)
    return {key: tuple(chats) for key, chats in grouped.items()}


@dataclass(frozen=True)
class NativeSource:
    locator: Path
    witness: Witness

    @property
    def activity(self) -> int:
        if isinstance(self.witness, FileWitness):
            return self.witness.mtime_ns
        # OpenCode timestamps are milliseconds; file activity is nanoseconds.
        return (
            max(
                value or 0
                for value in (
                    self.witness.session_updated_ms,
                    self.witness.message_updated_ms,
                    getattr(self.witness, "part_updated_ms", 0),
                )
            )
            * 1_000_000
        )


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
    parsed: dict[NativeKey, ParsedSessionTranscript] = field(
        default_factory=dict[NativeKey, ParsedSessionTranscript]
    )

    @classmethod
    def open(cls, runtime_root: Path, project_root: Path) -> SearchProjection:
        bindings = native_bindings(runtime_root)
        path = runtime_root / "history-index" / INDEX_FILENAME
        cold = not path.exists()
        try:
            try:
                index = NativeSearchIndex(path, timeout=0)
                stored = index.inventory()
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                    raise
                # No authoritative bytes live here. Recreate a damaged projection.
                discard_native_search_index(runtime_root)
                index = NativeSearchIndex(path, timeout=0)
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
            if stored and (stored.witness, stored.parser_version) == (
                witness_json(source.witness),
                PARSER_VERSION,
            ):
                if stored.status == "complete":
                    self.fresh.add(key)
                else:
                    self.errors[key] = "; ".join(stored.reasons) or stored.status

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
                    if (
                        not rebuild
                        and isinstance(source.witness, FileWitness)
                        and source.witness.size > LAZY_SOURCE_BYTES
                    ):
                        continue
                    budget = None if rebuild else TranscriptBudget(deadline, LAZY_SOURCE_BYTES)
                    target_source = TranscriptSource(
                        "opencode_db" if key.harness == "opencode" else "native_file",
                        key.session_id,
                        key.harness,
                        key.harness,
                        source.locator,
                    )
                    target = SessionLogTarget(
                        key.session_id, key.harness, source.locator, key.harness, (target_source,)
                    )
                    parse = partial(
                        parse_session_target,
                        project_root=self.project_root,
                        runtime_root=self.runtime_root,
                        target=target,
                        route=SessionLogRoute("ref", keys[key][0]),
                        budget=budget,
                    )
                    if key.harness == "opencode":
                        with read_opencode_search_source(source.locator, key.session_id) as (
                            w,
                            events,
                        ):
                            transcript = parse(events=events)
                        source = NativeSource(source.locator, w)
                        self.sources[key] = source
                    else:
                        transcript = parse()
                        if file_witness(source.locator) != source.witness:
                            continue
                    if budget and budget.exhausted:
                        continue
                    reasons = transcript.read_reasons
                    complete = transcript.search_ready and not reasons
                    if self.index:
                        self.index.timeout = min(2, max(0, deadline - time.monotonic()))
                        self.index.replace_source(
                            key,
                            locator=source.locator,
                            witness=source.witness,
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
                    else:
                        self.errors[key] = "; ".join(reasons) or "partial"
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.errors[key] = str(exc)

    def search(
        self, query: str, *, limit: int | None = 101, deadline: float | None = None
    ) -> list[SearchRow]:
        if self.index:
            return self.index.search(query, keys=self.fresh, limit=limit, deadline=deadline)[1]
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
