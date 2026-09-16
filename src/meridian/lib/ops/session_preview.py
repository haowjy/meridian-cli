"""Cache-first bounded session previews; canonical source reads stay outside index locks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.harness.transcript import transcript_revision
from meridian.lib.harness.transcript_preview import (
    TRANSCRIPT_PREVIEW_VERSION,
    PreviewAccumulator,
    TranscriptPreview,
)
from meridian.lib.ops.runtime import resolve_roots_for_read
from meridian.lib.ops.session_target import TranscriptSource, resolve_session_log_target
from meridian.lib.ops.session_transcript import iter_source_events
from meridian.lib.state import session_store
from meridian.lib.state.history import HistoryCursor, iter_history_events
from meridian.lib.state.history_changes import HistorySource
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.native_snapshot import TranscriptValidation
from meridian.lib.state.retention_archive import catalog_heads, read_receipts, verify_archive


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
    signatures: tuple[str, ...]
    selected: int
    preview: TranscriptPreview
    extent: int = 0
    source_size: int = 0
    device: int = 0
    inode: int = 0
    tail: str = ""
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
    return json.dumps(
        (source, transcript_revision(source.path)), default=str, separators=(",", ":")
    )


def _tail(path: Path, extent: int) -> str:
    with path.open("rb") as handle:
        handle.seek(max(0, extent - 256))
        return hashlib.sha256(handle.read(min(extent, 256))).hexdigest()


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
            "version" not in snapshot.preview.model_fields_set
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
                return resolve_session_log_target(
                    ref=identity.history_id or identity.ref,
                    file_path=None,
                    project_root=self.project_root,
                    runtime_root=self.roots.runtime_root if self.roots else None,
                )

            target = resolve()
            # Resolution may have rebuilt the missing metadata database.
            cached = self._cached(identity)
            old = cached[2] if cached else None
            signatures = tuple(_signature(source) for source in target.sources)
            if old is not None and old.complete and old.signatures == signatures:
                return old.view("current")
            snapshot = None
            archive_error: Exception | None = None
            for position, source in enumerate(target.sources):
                if not current():
                    return None
                accumulator = PreviewAccumulator()
                validation = TranscriptValidation()
                cursor = HistoryCursor()
                device = inode = source_size = 0
                tail = ""
                managed = source.kind == "spawn_history" and source.path is not None
                if managed:
                    assert source.path is not None
                    info = source.path.stat()
                    device, inode = info.st_dev, info.st_ino
                    source_size = info.st_size
                    if (
                        old is not None
                        and old.selected == position
                        and old.device == device
                        and old.inode == inode
                        and 0 < old.extent <= info.st_size
                        and old.source_size > 0
                        and (info.st_size > old.source_size or old.signatures == signatures)
                        and _tail(source.path, old.extent) == old.tail
                    ):
                        accumulator = PreviewAccumulator(old.preview)
                        cursor.extent = old.extent
                    events = iter_history_events(source.path, cursor=cursor, end=info.st_size)
                else:
                    events = iter_source_events(source, validation=validation, current=current)
                try:
                    for event in events:
                        if not current():
                            return None
                        accumulator.feed(event)
                    if source.kind == "archive":
                        assert source.path is not None and source.history_id is not None
                        verify_archive(
                            source.path,
                            manifest_sha256=source.manifest_sha256,
                            history_id=UUID(source.history_id),
                            current=current,
                        )
                except (ValueError, OSError, EOFError, zipfile.BadZipFile, zlib.error) as exc:
                    if source.kind != "archive":
                        raise
                    archive_error = exc
                    continue
                finally:
                    events.close()
                if not managed and validation.state != "complete":
                    return self.peek(identity) or PreviewView((), "updating")
                if managed:
                    assert source.path is not None
                    tail = _tail(source.path, cursor.extent)
                archive_digest = (
                    self.index.selected_archive_digest(identity.history_id)
                    if source.kind == "archive" and self.index and identity.history_id
                    else None
                )
                snapshot = _Snapshot(
                    signatures=signatures,
                    selected=position,
                    preview=accumulator.preview,
                    extent=cursor.extent,
                    source_size=source_size,
                    device=device,
                    inode=inode,
                    tail=tail,
                    archive_digest=archive_digest,
                    source=source.source_label,
                )
                if (
                    validation.header is not None
                    or accumulator.preview.has_interaction
                    or accumulator.preview.rendering_reason
                ):
                    break
            if snapshot is None:
                if archive_error:
                    raise archive_error
                raise FileNotFoundError("Transcript not available")
            if not current():
                return None
            latest_target = resolve()
            if latest_target.sources != target.sources:
                return self.peek(identity) or PreviewView((), "updating")
            chosen = target.sources[snapshot.selected]
            published_snapshot: _Snapshot = snapshot

            def prepare_value() -> str | None:
                nonlocal published_snapshot
                if not current() or not generation_current():
                    return None
                now = tuple(_signature(source) for source in target.sources)
                complete = now == signatures
                if chosen.kind == "archive" and self.roots:
                    selected = catalog_heads(read_receipts(self.roots.runtime_root))
                    if selected.get(identity.history_id or "") != published_snapshot.archive_digest:
                        return None
                if chosen.kind == "spawn_history" and chosen.path is not None:
                    if any(
                        before != after
                        for i, (before, after) in enumerate(zip(signatures, now, strict=True))
                        if i != published_snapshot.selected
                    ):
                        return None
                    info = chosen.path.stat()
                    if not (
                        info.st_dev == published_snapshot.device
                        and info.st_ino == published_snapshot.inode
                        and info.st_size >= published_snapshot.extent
                        and _tail(chosen.path, published_snapshot.extent) == published_snapshot.tail
                    ):
                        return None
                    if (
                        now[published_snapshot.selected] != signatures[published_snapshot.selected]
                        and info.st_size <= published_snapshot.source_size
                    ):
                        return None
                    complete &= info.st_size == published_snapshot.extent
                    published_snapshot = published_snapshot.model_copy(
                        update={"source_size": info.st_size}
                    )
                elif not complete:
                    return None
                published_snapshot = published_snapshot.model_copy(
                    update={"complete": complete, "signatures": now}
                )
                return published_snapshot.model_dump_json()

            if cached and self.index:
                source_lock = (
                    HistorySource(kind="spawn", key=chosen.path.parent.name)
                    if chosen.kind == "spawn_history" and chosen.path is not None
                    else HistorySource(
                        kind="sessions" if identity.history_id is None else "catalog"
                    )
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
