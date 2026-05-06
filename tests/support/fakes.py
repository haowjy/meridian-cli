"""Test doubles for deterministic behavior in unit and integration tests."""

from datetime import UTC, datetime


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
