"""Pi lifecycle sidecar file readers for spawned and primary flows."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Final

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.pi_lifecycle_events import parse_pi_lifecycle_event_line
from meridian.lib.launch.constants import (
    PI_LIFECYCLE_EVENT_FILE_ENV,
    PI_LIFECYCLE_EVENTS_FILENAME,
)

PI_LIFECYCLE_FILE_POLL_SECONDS: Final[float] = 0.05


class PiLifecycleEventTailer:
    """Incremental JSONL tailer for Pi lifecycle sidecar events."""

    def __init__(
        self,
        *,
        file_path: Path,
        spawn_id: SpawnId | str,
        harness_id: str,
    ) -> None:
        self._file_path = file_path
        self._spawn_id = str(spawn_id)
        self._harness_id = harness_id
        self._handle: BinaryIO | None = None
        self._offset = 0
        self._partial = b""

    @property
    def poll_interval_secs(self) -> float:
        return PI_LIFECYCLE_FILE_POLL_SECONDS

    def open(self) -> None:
        self._handle = self._file_path.open("rb")
        self._offset = 0
        self._partial = b""

    def close(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None

    def read_ready_events(self) -> list[HarnessEvent]:
        handle = self._handle
        if handle is None:
            raise RuntimeError("PiLifecycleEventTailer.read_ready_events() called before open()")

        handle.seek(self._offset)
        chunk = handle.read()
        if not chunk:
            return []

        self._offset += len(chunk)
        return self._parse_chunk(chunk)

    def catch_up_to_eof(self) -> list[HarnessEvent]:
        events: list[HarnessEvent] = []
        while True:
            ready = self.read_ready_events()
            if not ready:
                break
            events.extend(ready)
        return events

    def _parse_chunk(self, chunk: bytes) -> list[HarnessEvent]:
        buffer = self._partial + chunk
        if not buffer:
            return []

        if buffer.endswith(b"\n"):
            complete_lines = buffer.split(b"\n")[:-1]
            self._partial = b""
        else:
            pieces = buffer.split(b"\n")
            complete_lines = pieces[:-1]
            self._partial = pieces[-1]

        events: list[HarnessEvent] = []
        for line_bytes in complete_lines:
            if not line_bytes:
                continue
            line = line_bytes.decode("utf-8", errors="replace")
            event = parse_pi_lifecycle_event_line(
                line,
                expected_parent_spawn_id=self._spawn_id,
                harness_id=self._harness_id,
            )
            if event is not None:
                events.append(event)
        return events


def read_pi_lifecycle_events_file(
    *,
    file_path: Path,
    expected_parent_spawn_id: SpawnId | str,
    harness_id: str,
) -> list[HarnessEvent]:
    """Parse all complete lifecycle JSONL lines from one sidecar file snapshot."""

    if not file_path.is_file():
        return []

    tailer = PiLifecycleEventTailer(
        file_path=file_path,
        spawn_id=expected_parent_spawn_id,
        harness_id=harness_id,
    )
    tailer.open()
    try:
        return tailer.catch_up_to_eof()
    finally:
        tailer.close()


def prepare_pi_lifecycle_event_file(
    *,
    spawn_dir: Path,
    env: dict[str, str],
) -> Path:
    """Create lifecycle sidecar file and wire env override for Pi extensions."""

    lifecycle_path = spawn_dir / PI_LIFECYCLE_EVENTS_FILENAME
    lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_path.touch(exist_ok=True)
    env[PI_LIFECYCLE_EVENT_FILE_ENV] = str(lifecycle_path)
    return lifecycle_path


__all__ = [
    "PI_LIFECYCLE_FILE_POLL_SECONDS",
    "PiLifecycleEventTailer",
    "prepare_pi_lifecycle_event_file",
    "read_pi_lifecycle_events_file",
]
