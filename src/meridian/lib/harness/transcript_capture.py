"""Qualify a native transcript observation for sealed snapshot publication.

Provider-owned grammar: complete, known-incomplete, unavailable, or unsupported.
Publication happens only after a complete observation of the declared scope.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

from meridian.lib.harness.capture_qualify import CaptureObserver, observer_for
from meridian.lib.harness.opencode_transcript import iter_opencode_db_events
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.native_snapshot import (
    SnapshotObservation,
    SnapshotRecord,
    SourceRevision,
    read_jsonl_frame,
    reject_unframed_storage_frame,
)

CaptureStatus = Literal["complete", "known-incomplete", "unavailable", "unsupported"]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


_DIALECT = {
    "pi": "pi.session",
    "claude": "claude.jsonl",
    "codex": "codex.rollout",
    "opencode": "opencode.transcript.v1",
}
_SCOPE = "native-session"


class CaptureIncomplete(ValueError):
    """Native source is not a complete observation; do not publish a seal."""


@dataclass
class NativeCapture:
    source: str
    dialect: str
    harness: str
    session_id: str
    kind: str
    path: Path | None
    scope: str = _SCOPE
    status: CaptureStatus = "complete"
    reason: str | None = None
    observed_from: str = field(default_factory=utc_now_iso)
    _sha256: _Digest = field(default_factory=hashlib.sha256)
    _count: int = 0

    def fail(self, status: CaptureStatus, reason: str) -> None:
        if self.status == "complete":
            self.status = status
            self.reason = reason

    def _record(self, raw: str) -> SnapshotRecord:
        record = SnapshotRecord(source=self.source, ordinal=self._count, raw=raw)
        self._sha256.update(raw.encode("utf-8"))
        self._count += 1
        return record

    def records(self) -> Iterator[SnapshotRecord]:
        if self.status != "complete":
            return
        observer = observer_for(self.harness, self.session_id)
        if self.kind == "opencode_db":
            yield from self._opencode_records(observer)
            return
        if self.path is None:
            raise FileNotFoundError(f"Session file for '{self.session_id}' not found")
        yield from self._jsonl_records(self.path, observer)

    def finish(self) -> SnapshotObservation:
        if self.status != "complete":
            raise CaptureIncomplete(self.reason or self.status)
        return SnapshotObservation(
            observed_until=utc_now_iso(),
            sources=(
                SourceRevision(
                    source=self.source,
                    sha256=self._sha256.hexdigest(),
                    records=self._count,
                ),
            ),
        )

    def _jsonl_records(self, path: Path, observer: CaptureObserver) -> Iterator[SnapshotRecord]:
        before = _revision(path)
        with path.open("rb") as handle:
            while True:
                raw = read_jsonl_frame(handle)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    raise ValueError("Incomplete JSONL frame")
                stripped = raw.strip()
                if not stripped:
                    continue
                reject_unframed_storage_frame(stripped)
                text = raw.decode("utf-8")
                try:
                    payload = json.loads(stripped.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError("Malformed JSONL record") from exc
                if isinstance(payload, dict):
                    observer.observe(cast("dict[str, object]", payload))
                yield self._record(text)
        if _revision(path) != before:
            self.fail("unavailable", "Native source changed during capture")
            return
        self._qualify(observer)

    def _opencode_records(self, observer: CaptureObserver) -> Iterator[SnapshotRecord]:
        for event in iter_opencode_db_events(session_id=self.session_id, db_path=self.path):
            observer.observe(event)
            raw = (
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            yield self._record(raw)
        self._qualify(observer)

    def _qualify(self, observer: CaptureObserver) -> None:
        if self.status != "complete":
            return
        reason = observer.incomplete_reason()
        if reason is not None:
            self.fail("known-incomplete", reason)


def native_capture(
    *,
    kind: str,
    harness: str | None,
    session_id: str,
    path: Path | None,
) -> NativeCapture:
    normalized = (harness or "").strip().lower()
    dialect = _DIALECT.get(normalized)
    capture = NativeCapture(
        source=session_id,
        dialect=dialect or "unsupported",
        harness=normalized,
        session_id=session_id,
        kind=kind,
        path=path,
    )
    if dialect is None:
        capture.fail(
            "unsupported",
            f"Native capture is unsupported for harness {normalized!r}",
        )
    elif kind not in {"native_file", "opencode_db"}:
        capture.fail("unsupported", f"Native capture is unsupported for source kind {kind!r}")
    elif kind == "native_file" and path is None:
        raise FileNotFoundError(f"Session file for '{session_id}' not found")
    return capture


def _revision(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
