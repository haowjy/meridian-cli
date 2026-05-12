"""Test doubles for deterministic behavior in unit and integration tests."""

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from meridian.lib.telemetry.events import TelemetryEnvelope


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now

    def utc_now_iso(self) -> str:
        return (
            datetime.fromtimestamp(self._now, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeHeartbeat:
    """Test double for heartbeat touch operations."""

    def __init__(self) -> None:
        self.touches: list[float] = []
        self._clock: FakeClock | None = None

    def set_clock(self, clock: FakeClock) -> None:
        self._clock = clock

    def touch(self) -> None:
        timestamp = self._clock.time() if self._clock is not None else 0.0
        self.touches.append(timestamp)


class RecordingTelemetrySink:
    """In-process telemetry sink for integration tests.

    Shared by test_spawn_manager.py and test_spawn_api.py (and future callers).
    Replaces the per-file copies that were duplicated across those modules.
    """

    def __init__(self) -> None:
        self.events: list[TelemetryEnvelope] = []

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        self.events.extend(events)

    def close(self) -> None:
        pass


def wait_for_telemetry(
    predicate: Callable[[], bool],
    timeout: float = 1.0,
) -> None:
    """Poll until a telemetry predicate is true, or raise AssertionError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("telemetry event not observed within timeout")
