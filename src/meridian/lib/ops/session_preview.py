"""Cache-first bounded session previews; canonical source reads stay outside index locks."""

from __future__ import annotations

import json
import sqlite3
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.harness.native_witness import file_witness
from meridian.lib.harness.transcript_preview import (
    TRANSCRIPT_PREVIEW_VERSION,
    PreviewAccumulator,
    TranscriptPreview,
)
from meridian.lib.ops.runtime import resolve_roots_for_read
from meridian.lib.ops.session_target import TranscriptSource, resolve_transcript_source
from meridian.lib.ops.session_transcript import TranscriptBudget, read_native_source
from meridian.lib.state import session_store
from meridian.lib.state.history_changes import HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.retention_archive import catalog_heads, read_receipts


@dataclass(frozen=True)
class PreviewIdentity:
    ref: str
    history_id: str | None = None
    generation: str = ""

    @property
    def key(self) -> str:
        return json.dumps((self.history_id, self.generation, self.ref), separators=(",", ":"))


@dataclass(frozen=True)
class PreviewView:
    lines: tuple[str, ...]
    state: Literal["current", "updating", "offline", "unavailable"]
    cached: bool = False
    source: str = ""
    omitted_messages: bool = False
    clipped_text: bool = False

    @property
    def status(self) -> str:
        label = "archive offline" if self.state == "offline" else self.state
        if self.cached:
            label = f"cached · {label}"
        if self.omitted_messages or self.clipped_text:
            label += " · clipped"
        return label

    @property
    def detail(self) -> str:
        parts: list[str] = []
        if self.omitted_messages:
            parts.append("earlier context omitted")
        if self.clipped_text:
            parts.append("text clipped")
        if self.source:
            parts.append(self.source)
        return " · ".join(parts)


class _Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: Literal[2] = 2
    signatures: tuple[str, ...]
    selected: int
    preview: TranscriptPreview
    archive_digest: str | None = None
    complete: bool = True  # Source read/consistency; rendering support is separate.
    source: str = ""

    def view(
        self,
        state: Literal["current", "updating", "offline", "unavailable"],
        *,
        cached: bool = False,
    ) -> PreviewView:
        return PreviewView(
            self.preview.lines(),
            "unavailable" if self.preview.rendering_reason and state != "offline" else state,
            cached,
            self.source,
            self.preview.omitted_messages,
            self.preview.clipped_text,
        )


def _signature(source: TranscriptSource) -> str:
    paths = [source.path]
    if source.kind == "opencode_db":
        paths.append(Path(f"{source.path}-wal"))
    revisions = tuple(file_witness(path).encode() if path.exists() else None for path in paths)
    return json.dumps((source, revisions), default=str, separators=(",", ":"))


class SessionPreview:
    """One normal/warm-rebuild projector, with a source-free cache read for the UI."""

    def __init__(self, project_root: str) -> None:
        self.roots = resolve_roots_for_read(project_root)
        self.project_root = Path(project_root)
        self.index = HistoryIndex(self.roots.runtime_root) if self.roots else None

    def _cached(self, identity: PreviewIdentity) -> tuple[str, str | None, _Snapshot | None] | None:
        if self.index is None:
            return None
        try:
            cached = self.index.preview_cache(identity.key)
        except (OSError, ValueError, sqlite3.Error):
            return None
        if cached is None:
            return None
        build, value = cached
        try:
            snapshot = _Snapshot.model_validate_json(value) if value else None
        except ValidationError:
            snapshot = None
        if snapshot is not None and (
            "version" not in snapshot.model_fields_set
            or "version" not in snapshot.preview.model_fields_set
            or snapshot.preview.version != TRANSCRIPT_PREVIEW_VERSION
        ):
            snapshot = None
        return build, value, snapshot

    def peek(self, identity: PreviewIdentity) -> PreviewView | None:
        cached = self._cached(identity)
        return cached[2].view("updating", cached=True) if cached and cached[2] else None

    def refresh(self, identity: PreviewIdentity, current: Callable[[], bool]) -> PreviewView | None:
        try:

            def generation_current() -> bool:
                if identity.history_id is not None:
                    return True
                if not self.roots or not identity.generation:
                    return False
                session = session_store.get_session_record(self.roots.runtime_root, identity.ref)
                return session is not None and identity.generation == (
                    session.session_instance_id or session.harness_session_id or session.started_at
                )

            def resolve():
                if not generation_current():
                    raise ValueError(
                        "Selected session generation changed; refresh the session list"
                    )
                return resolve_transcript_source(
                    ref=identity.history_id or identity.ref,
                    file_path=None,
                    project_root=self.project_root,
                    runtime_root=self.roots.runtime_root if self.roots else None,
                )

            target = resolve()
            # Resolution may have rebuilt the missing metadata database.
            cached = self._cached(identity)
            old = cached[2] if cached else None
            signatures = (_signature(target.source),)
            if old is not None and old.complete and old.signatures == signatures:
                return old.view("current")
            if not current():
                return None
            source = target.source
            accumulator = PreviewAccumulator()
            read = read_native_source(
                source, budget=TranscriptBudget(float("inf"), 2**63, selected=current)
            )
            try:
                for event in read.events:
                    if not current():
                        return None
                    accumulator.feed(event)
            finally:
                read.events.close()
            if read.reasons:
                return PreviewView(
                    tuple(f"partial: {reason}" for reason in read.reasons), "unavailable"
                )
            if read.validation.state != "complete":
                return self.peek(identity) or PreviewView((), "updating")
            archive_digest = (
                self.index.selected_archive_digest(identity.history_id)
                if self.index and identity.history_id
                else None
            )
            snapshot = _Snapshot(
                signatures=signatures,
                selected=0,
                preview=accumulator.preview,
                archive_digest=archive_digest,
                source=target.view_label or source.source_label,
            )
            if not current():
                return None
            latest_target = resolve()
            if latest_target.source != target.source:
                return self.peek(identity) or PreviewView((), "updating")
            published_snapshot: _Snapshot = snapshot

            def prepare_value() -> str | None:
                nonlocal published_snapshot
                if not current() or not generation_current():
                    return None
                now = (_signature(target.source),)
                complete = now == signatures
                if identity.history_id and self.roots:
                    selected = catalog_heads(read_receipts(self.roots.runtime_root))
                    if selected.get(identity.history_id or "") != published_snapshot.archive_digest:
                        return None
                if not complete:
                    return None
                published_snapshot = published_snapshot.model_copy(
                    update={"complete": complete, "signatures": now}
                )
                return published_snapshot.model_dump_json()

            if cached and self.index:
                source_lock = HistorySource(
                    kind="sessions" if identity.history_id is None else "catalog"
                )
                stored = self.index.store_preview(
                    identity.key,
                    build=cached[0],
                    previous=cached[1],
                    history_id=identity.history_id,
                    archive_digest=snapshot.archive_digest,
                    source=source_lock,
                    prepare_value=prepare_value,
                )
                if not stored:
                    return self.peek(identity) or PreviewView((), "updating")
            elif prepare_value() is None:
                return PreviewView((), "updating")
            return published_snapshot.view("current" if published_snapshot.complete else "updating")
        except (
            ValueError,
            OSError,
            EOFError,
            sqlite3.Error,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            fallback = self._cached(identity)
            old = fallback[2] if fallback else None
            if old is not None:
                state = (
                    "offline"
                    if old.archive_digest and isinstance(exc, FileNotFoundError)
                    else "unavailable"
                )
                return old.view(state, cached=True)
            return PreviewView((str(exc) or "Preview unavailable",), "unavailable")
