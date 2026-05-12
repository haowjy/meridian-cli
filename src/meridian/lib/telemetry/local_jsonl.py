"""Local JSONL telemetry sink with compound owner/PID rotating segments."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from meridian.lib.telemetry.events import TelemetryEnvelope

_DEFAULT_MAX_SEGMENT_BYTES = 10_000_000


class LocalJSONLSink:
    """Append telemetry envelopes to per-process JSONL segment files."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        max_segment_bytes: int = _DEFAULT_MAX_SEGMENT_BYTES,
        logical_owner: str | None = None,
    ) -> None:
        self.telemetry_dir = runtime_root / "telemetry"
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self._logical_owner = logical_owner or "cli"
        self._pid = os.getpid()
        self._seq = self._next_sequence()
        self._max_segment_bytes = max_segment_bytes
        self._file: TextIO | None = None
        self._closed = False
        self._open_segment()

    @property
    def active_path(self) -> Path:
        """Return the current active segment path."""
        return self.telemetry_dir / (f"{self._logical_owner}.{self._pid}-{self._seq:04d}.jsonl")

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        """Append compact JSON lines to the active segment."""
        if self._closed:
            return
        file = self._ensure_open()
        for event in events:
            try:
                line = json.dumps(event.to_dict(), separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            file.write(line)
            file.write("\n")
        file.flush()
        if self.active_path.stat().st_size > self._max_segment_bytes:
            self._rotate()

    def close(self) -> None:
        """Flush and close the active segment. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None

    def _next_sequence(self) -> int:
        seqs: list[int] = []
        pattern = f"{self._logical_owner}.{self._pid}-*.jsonl"
        for path in self.telemetry_dir.glob(pattern):
            try:
                seqs.append(int(path.stem.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        return max(seqs, default=0) + 1

    def _open_segment(self) -> None:
        self._file = self.active_path.open("a", encoding="utf-8")

    def _ensure_open(self) -> TextIO:
        if self._file is None:
            self._open_segment()
        assert self._file is not None
        return self._file

    def _rotate(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._seq += 1
        self._open_segment()
