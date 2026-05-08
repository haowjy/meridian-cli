"""Shared harness history read/write helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.state.atomic import append_text_line
from meridian.lib.state.managed_primary import ManagedPrimaryCausalTracker


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
    _seq: int = field(default=0, init=False)
    _byte_offset: int = field(default=0, init=False)
    _causal_tracker: ManagedPrimaryCausalTracker = field(
        default_factory=ManagedPrimaryCausalTracker,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.history_path.exists():
            return
        content = self.history_path.read_bytes()
        last_complete_line_end = content.rfind(b"\n") + 1
        self._byte_offset = last_complete_line_end
        self._seq = content[:last_complete_line_end].count(b"\n")
        self._rehydrate_causal_tracker(content[:last_complete_line_end])
        if last_complete_line_end < len(content):
            with self.history_path.open("r+b") as handle:
                handle.truncate(last_complete_line_end)

    @property
    def last_seq(self) -> int:
        """Last written sequence number (0-indexed)."""
        if self._seq == 0:
            return -1
        return self._seq - 1

    def write(self, event: HarnessEvent) -> WriteResult:
        """Write one event and return write success metadata."""

        causal = self._causal_tracker.derive(event)
        envelope = {
            "seq": self._seq,
            "byte_offset": self._byte_offset,
            "turn_id": causal.turn_id,
            "item_id": causal.item_id,
            "request_id": causal.request_id,
            "interrupt_epoch": causal.interrupt_epoch,
            "stale_after_interrupt": causal.stale_after_interrupt,
            "event_type": event.event_type,
            "harness_id": event.harness_id,
            "payload": event.payload,
            "raw_text": event.raw_text,
        }
        line = json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"

        try:
            append_text_line(self.history_path, line)
        except Exception as exc:  # pragma: no cover - return-path tested
            return WriteResult(success=False, error=str(exc))

        assigned_seq = self._seq
        self._seq += 1
        self._byte_offset += len(line.encode("utf-8"))
        return WriteResult(success=True, seq=assigned_seq)

    def _rehydrate_causal_tracker(self, content: bytes) -> None:
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
                HarnessEvent(
                    event_type=event_type,
                    payload=replay_payload,
                    harness_id=harness_id,
                )
            )


def iter_history_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield seq-enveloped event dictionaries from a history JSONL file."""

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # Crash-only tolerance for truncated/corrupt trailing lines.
            continue
        if isinstance(payload, dict):
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
            "turn_id",
            "item_id",
            "request_id",
            "interrupt_epoch",
            "stale_after_interrupt",
        )
    }


def _read_legacy_output_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def read_spawn_events(spawn_dir: Path) -> Iterator[dict[str, Any]]:
    """Read spawn events from history.jsonl, falling back to output.jsonl."""

    history_path = spawn_dir / HISTORY_FILENAME
    if history_path.exists():
        yield from iter_history_events(history_path)
        return
    yield from _read_legacy_output_events(spawn_dir / OUTPUT_FILENAME)


__all__ = [
    "HarnessHistoryWriter",
    "WriteResult",
    "iter_history_events",
    "iter_history_from_seq",
    "read_history_range",
    "read_spawn_events",
    "strip_seq_envelope",
]
