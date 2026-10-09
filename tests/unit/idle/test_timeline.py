from dataclasses import dataclass

import pytest

from meridian.lib.idle.timeline import Schedule, schedule


@dataclass(frozen=True)
class Config:
    push_seconds: int = 60
    warn_minutes: int = 15
    compact_minutes: int = 5


@pytest.mark.parametrize(
    ("ttl_seconds", "expected"),
    [
        (3600, Schedule(push_at=60_000, warn_at=2_700_000, compact_at=3_300_000)),
        (1800, Schedule(push_at=60_000, warn_at=900_000, compact_at=1_500_000)),
        (300, Schedule(push_at=60_000)),
        (None, Schedule(push_at=60_000)),
    ],
    ids=["one-hour", "thirty-minutes", "five-minutes", "unknown"],
)
def test_schedule_places_only_windows_that_fit(
    ttl_seconds: int | None,
    expected: Schedule,
) -> None:
    assert schedule(0, ttl_seconds, Config()) == expected


def test_schedule_uses_strict_stage_order() -> None:
    cfg = Config(push_seconds=60, warn_minutes=4, compact_minutes=4)

    assert schedule(0, 300, cfg) == Schedule(push_at=60_000)
