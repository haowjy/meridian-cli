"""Shared harness history read/write helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.state.atomic import append_text_line, atomic_write_bytes, atomic_write_text
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_codec import (
    TranscriptHeader,
    transcript_header,
)
from meridian.lib.state.managed_primary import ManagedPrimaryCausalTracker
from meridian.lib.state.spawn.repository import read_state
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

logger = logging.getLogger(__name__)

_LAST_OBSERVED_EVENT_CHECKPOINT_INTERVAL_SECONDS = 1.0
_RAW_UNPARSED_MAX_LENGTH = 4096
_RAW_UNPARSED_TRUNCATION_MARKER = "\n[truncated by Meridian]"


@dataclass(frozen=True)
class WriteResult:
    """Result envelope for append attempts."""

    success: bool
    seq: int = -1
    error: str | None = None


@dataclass
class HarnessHistoryWriter:
    """Append-only writer for seq-enveloped raw harness events."""

    history_path: Path
    last_observed_event_path: Path | None = None
    clock: Clock = field(default_factory=RealClock)
    runtime_root: Path | None = None
    spawn_id: str | None = None
    _seq: int = field(default=0, init=False)
    _byte_offset: int = field(default=0, init=False)
    _causal_tracker: ManagedPrimaryCausalTracker = field(
        default_factory=ManagedPrimaryCausalTracker,
        init=False,
    )
    _event_counts: dict[str, int] = field(default_factory=dict, init=False)
    _last_marker_checkpoint_monotonic: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (self.runtime_root is None) is not (self.spawn_id is None):
            raise ValueError("runtime_root and spawn_id must be provided together")
        if self.runtime_root is not None and self.spawn_id is not None:
            mutate_published_spawn_artifact(
                self.runtime_root, SpawnId(self.spawn_id), self._initialize
            )
        else:
            self._initialize()

    def _mark_changed(self) -> None:
        if self.runtime_root is None or self.spawn_id is None:
            return
        state = read_state(self.runtime_root / "spawns", self.spawn_id, include_prompt=False)
        if state is None:
            raise FileNotFoundError("spawn no longer published")
        HistoryChanges(self.runtime_root).mark(
            HistorySource(kind="spawn", key=self.spawn_id),
            coalesce=state.status not in TERMINAL_SPAWN_STATUSES,
        )

    def _initialize(self) -> None:
        content = self.history_path.read_bytes() if self.history_path.exists() else b""
        header: TranscriptHeader | None = None
        if not content and self.runtime_root is not None and self.spawn_id is not None:
            state = read_state(self.runtime_root / "spawns", self.spawn_id, include_prompt=False)
            if state is not None and state.history_id is not None:
                header = transcript_header(state, self.runtime_root.name)
                self._mark_changed()
                content = (header.model_dump_json() + "\n").encode()
                atomic_write_bytes(self.history_path, content)
        last_complete_line_end = content.rfind(b"\n") + 1
        complete = content[:last_complete_line_end]
        lines = complete.splitlines(keepends=True)
        if lines:
            try:
                first = json.loads(lines[0])
            except (ValueError, UnicodeDecodeError):
                first = None
            if isinstance(first, dict) and first.get("record") == "meridian.transcript":
                header = TranscriptHeader.model_validate(first)
                if self.runtime_root is not None and self.spawn_id is not None:
                    state = read_state(
                        self.runtime_root / "spawns", self.spawn_id, include_prompt=False
                    )
                    if state is not None and state.history_id != header.history_id:
                        raise ValueError("Transcript history identity does not match its record")
                offset = len(lines[0])
                for seq, line in enumerate(lines[1:]):
                    event = json.loads(line)
                    if event.get("seq") != seq or event.get("byte_offset") != offset:
                        raise ValueError("Noncontiguous portable transcript")
                    offset += len(line)
        self._byte_offset = last_complete_line_end
        self._seq = len(lines) - (1 if header else 0)
        self._rehydrate_history_state(complete)
        if last_complete_line_end < len(content):
            self._mark_changed()
            atomic_write_bytes(self.history_path, complete)

    @property
    def last_seq(self) -> int:
        """Last written sequence number (0-indexed)."""
        if self._seq == 0:
            return -1
        return self._seq - 1

    def write(self, event: RawHarnessEvent) -> WriteResult:
        """Write one event and return write success metadata."""

        causal = self._causal_tracker.derive(event)
        timestamp = self.clock.utc_now_iso()
        envelope: dict[str, object] = {
            "seq": self._seq,
            "byte_offset": self._byte_offset,
            "timestamp": timestamp,
            "interrupt_epoch": causal.interrupt_epoch,
            "event_type": event.event_type,
            "harness_id": event.harness_id,
            "payload": event.payload,
        }
        for key, value in (
            ("turn_id", causal.turn_id),
            ("item_id", causal.item_id),
            ("request_id", causal.request_id),
        ):
            if value is not None:
                envelope[key] = value
        if causal.stale_after_interrupt:
            envelope["stale_after_interrupt"] = True
        meta = _wire_envelope_meta(event)
        if meta:
            envelope["meta"] = meta
        line = json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"

        assigned_seq = self._seq

        def _write_artifacts() -> None:
            self._mark_changed()
            append_text_line(self.history_path, line)
            self._seq += 1
            self._byte_offset += len(line.encode("utf-8"))
            self._record_last_observed_event(event, timestamp=timestamp, seq=assigned_seq)

        try:
            if self.runtime_root is None or self.spawn_id is None:
                _write_artifacts()
            elif not mutate_published_spawn_artifact(
                self.runtime_root,
                SpawnId(self.spawn_id),
                _write_artifacts,
            ):
                return WriteResult(success=False, error="spawn no longer published")
        except Exception as exc:  # pragma: no cover - return-path tested
            return WriteResult(success=False, error=str(exc))

        return WriteResult(success=True, seq=assigned_seq)

    def _record_last_observed_event(
        self,
        event: RawHarnessEvent,
        *,
        timestamp: str,
        seq: int,
    ) -> None:
        event_kind = event.event_type.lower().replace(".", "/")
        if event_kind in {"turn/started", "turn/completed", "item/started", "item/completed"}:
            self._event_counts[event_kind] = self._event_counts.get(event_kind, 0) + 1
        if self.last_observed_event_path is None:
            return
        marker = {
            "event_kind": event.event_type,
            "timestamp": timestamp,
            "seq": seq,
            "turn_started": self._event_counts.get("turn/started", 0),
            "turn_completed": self._event_counts.get("turn/completed", 0),
            "item_started": self._event_counts.get("item/started", 0),
            "item_completed": self._event_counts.get("item/completed", 0),
        }
        now = self.clock.monotonic()
        last_checkpoint = self._last_marker_checkpoint_monotonic
        if (
            last_checkpoint is not None
            and (now - last_checkpoint) < _LAST_OBSERVED_EVENT_CHECKPOINT_INTERVAL_SECONDS
        ):
            return
        try:
            atomic_write_text(
                self.last_observed_event_path,
                json.dumps(marker, separators=(",", ":"), sort_keys=True) + "\n",
            )
        except Exception:
            # History remains authoritative. Diagnostic marker loss must not stop
            # event consumption or cause the already-appended event to be replayed.
            logger.warning("Failed to update last-observed-event marker", exc_info=True)
            return
        self._last_marker_checkpoint_monotonic = now

    def _rehydrate_history_state(self, content: bytes) -> None:
        for line in content.decode("utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                envelope = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(envelope, dict):
                continue
            typed_envelope = cast("dict[str, object]", envelope)
            event_type = typed_envelope.get("event_type")
            payload = typed_envelope.get("payload")
            harness_id = typed_envelope.get("harness_id")
            if (
                not isinstance(event_type, str)
                or not event_type.strip()
                or not isinstance(payload, dict)
                or not isinstance(harness_id, str)
                or not harness_id.strip()
            ):
                continue

            normalized_event_type = event_type.lower().replace(".", "/")
            if normalized_event_type in {
                "turn/started",
                "turn/completed",
                "item/started",
                "item/completed",
            }:
                self._event_counts[normalized_event_type] = (
                    self._event_counts.get(normalized_event_type, 0) + 1
                )

            replay_payload: dict[str, object] = dict(cast("dict[str, object]", payload))
            for causal_key in (
                "turn_id",
                "item_id",
                "request_id",
                "interrupt_epoch",
                "stale_after_interrupt",
            ):
                if causal_key in typed_envelope and causal_key not in replay_payload:
                    replay_payload[causal_key] = typed_envelope[causal_key]
            self._causal_tracker.derive(
                RawHarnessEvent(
                    event_type=event_type,
                    payload=replay_payload,
                    harness_id=harness_id,
                )
            )


def _wire_envelope_meta(event: RawHarnessEvent) -> dict[str, object]:
    """Return wire fields not already represented by the normalized payload."""

    if event.raw_text is None:
        return {}
    try:
        parsed = json.loads(event.raw_text)
    except (json.JSONDecodeError, TypeError):
        return {"raw_unparsed": _bounded_unparsed_wire(event.raw_text)}
    if not isinstance(parsed, dict):
        return {"raw_unparsed": _bounded_unparsed_wire(event.raw_text)}

    wire = cast("dict[str, object]", parsed)
    for payload_key in ("payload", "params"):
        if wire.get(payload_key) == event.payload:
            return {key: value for key, value in wire.items() if key != payload_key}
    if wire == event.payload:
        return {}
    return {
        key: value
        for key, value in wire.items()
        if key not in event.payload or event.payload[key] != value
    }


def _bounded_unparsed_wire(raw_text: str) -> str:
    if len(raw_text) <= _RAW_UNPARSED_MAX_LENGTH:
        return raw_text
    prefix_length = _RAW_UNPARSED_MAX_LENGTH - len(_RAW_UNPARSED_TRUNCATION_MARKER)
    return raw_text[:prefix_length] + _RAW_UNPARSED_TRUNCATION_MARKER


@dataclass
class HistoryCursor:
    """Complete-line byte extent for an append-only history projection."""

    extent: int = 0


def iter_history_events(
    path: Path, *, cursor: HistoryCursor | None = None, end: int | None = None
) -> Generator[dict[str, Any]]:
    """Yield complete history events, optionally resuming a bounded byte snapshot."""
    if not path.exists():
        return
    cursor = cursor if cursor is not None else HistoryCursor()
    with path.open("rb") as handle:
        handle.seek(cursor.extent)
        while end is None or handle.tell() < end:
            line = handle.readline(-1 if end is None else end - handle.tell())
            if not line.endswith(b"\n"):
                break
            cursor.extent = handle.tell()
            stripped = line.decode("utf-8", errors="ignore").strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("record") != "meridian.transcript":
                yield cast("dict[str, Any]", payload)


def iter_history_from_seq(
    path: Path,
    *,
    start_seq: int = 0,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield events from start_seq, optionally limited.

    Unlike read_history_range(), this is lazy so callers can stream through
    histories without loading all events into memory.
    """

    yielded = 0
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                envelope = json.loads(stripped)
            except json.JSONDecodeError:
                # Crash-only tolerance for truncated/corrupt trailing lines.
                continue
            if not isinstance(envelope, dict):
                continue

            envelope = cast("dict[str, Any]", envelope)
            seq = envelope.get("seq", -1)
            if not isinstance(seq, int) or seq < start_seq:
                continue
            yield envelope
            yielded += 1
            if limit is not None and yielded >= limit:
                break


def read_history_range(
    path: Path,
    *,
    start_seq: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read a seq range from history.jsonl."""

    events: list[dict[str, Any]] = []
    for envelope in iter_history_events(path):
        seq = envelope.get("seq", -1)
        if not isinstance(seq, int) or seq < start_seq:
            continue
        events.append(envelope)
        if limit is not None and len(events) >= limit:
            break
    return events


def strip_seq_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Strip seq metadata and return the raw harness event shape."""

    return {
        key: value
        for key, value in envelope.items()
        if key
        not in (
            "seq",
            "byte_offset",
            "timestamp",
            "turn_id",
            "item_id",
            "request_id",
            "interrupt_epoch",
            "stale_after_interrupt",
        )
    }


__all__ = [
    "HarnessHistoryWriter",
    "WriteResult",
    "iter_history_events",
    "iter_history_from_seq",
    "read_history_range",
    "strip_seq_envelope",
]


def ingest_portable_history(
    runtime_root: Path,
    spawn_id: str,
    events: Iterator[dict[str, object]],
) -> bool:
    """Retain an otherwise harness-owned transcript once, after its process stops."""
    from meridian.lib.platform.atomic import atomic_replace

    def retain() -> None:
        state = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
        if state is None or state.history_id is None or state.status not in TERMINAL_SPAWN_STATUSES:
            raise ValueError("Native transcript capture requires an identified terminal record")
        path = runtime_root / "spawns" / spawn_id / "history.jsonl"
        if path.exists():
            return
        header = transcript_header(state, runtime_root.name)
        stamp = state.terminal.finished_at if state.terminal else state.started_at or ""
        HistoryChanges(runtime_root).mark(HistorySource(kind="spawn", key=spawn_id))
        with atomic_replace(path, mode="wb", encoding=None, permissions=0o600) as handle:
            line = (header.model_dump_json() + "\n").encode()
            handle.write(line)
            offset = len(line)
            for seq, payload in enumerate(events):
                line = (
                    json.dumps(
                        {
                            "seq": seq,
                            "byte_offset": offset,
                            "timestamp": stamp,
                            "interrupt_epoch": 0,
                            "event_type": "retained/native",
                            "harness_id": state.harness,
                            "payload": payload,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
                handle.write(line)
                offset += len(line)

    return mutate_published_spawn_artifact(runtime_root, SpawnId(spawn_id), retain)
