from __future__ import annotations

import json
import sys
import threading
from collections import deque
from collections.abc import Sequence
from typing import Protocol

from meridian.lib.telemetry.events import TelemetryEnvelope


class TelemetrySink(Protocol):
    """Best-effort telemetry event sink."""

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        """Write a batch of events."""
        ...

    def close(self) -> None:
        """Flush and release resources. Idempotent."""
        ...


class BufferingSink:
    """Buffering sink that upgrades to a real sink in-place."""

    def __init__(self, *, max_buffer: int = 1000) -> None:
        self._buffer: deque[TelemetryEnvelope] = deque(maxlen=max_buffer)
        self._real_sink: TelemetrySink | None = None
        self._lock = threading.Lock()

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        """Buffer events until upgraded, then forward to the real sink."""
        with self._lock:
            if self._real_sink is not None:
                self._real_sink.write_batch(events)
            else:
                self._buffer.extend(events)

    def upgrade(self, sink: TelemetrySink) -> None:
        """Atomically replay buffer and switch to the real sink."""
        with self._lock:
            if list(self._buffer):
                sink.write_batch(list(self._buffer))
            self._buffer.clear()
            self._real_sink = sink

    def close(self) -> None:
        """Close the real sink when one has been installed."""
        with self._lock:
            if self._real_sink is not None:
                self._real_sink.close()


class NoopSink:
    """Sink that discards all events."""

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        """Discard a batch of events."""
        _ = events

    def close(self) -> None:
        """No-op close."""


class StderrSink:
    """Structured JSON telemetry to stderr for rootless processes."""

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        """Write compact JSON envelopes to stderr."""
        for event in events:
            print(json.dumps(event.to_dict(), separators=(",", ":")), file=sys.stderr)

    def close(self) -> None:
        """No-op close."""
