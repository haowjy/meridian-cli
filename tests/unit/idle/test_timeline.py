from dataclasses import dataclass

import pytest

from meridian.lib.idle.timeline import Schedule, schedule


@dataclass(frozen=True)
class Config:
    warn_minutes: int = 15
    compact_minutes: int = 5


@pytest.mark.parametrize(
    ("ttl_seconds", "expected"),
    [
        (3600, Schedule(push_at=183_000, warn_at=2_823_000, compact_at=3_423_000)),
        (1800, Schedule(push_at=183_000, warn_at=1_023_000, compact_at=1_623_000)),
        (300, Schedule(push_at=183_000)),
        (None, Schedule(push_at=183_000)),
    ],
    ids=["one-hour", "thirty-minutes", "five-minutes", "unknown"],
)
def test_schedule_places_only_windows_that_fit(
    ttl_seconds: int | None,
    expected: Schedule,
) -> None:
    assert schedule(123_000, ttl_seconds, Config(), push_delay_seconds=60) == expected


def test_schedule_places_long_turn_push_after_push_delay() -> None:
    assert schedule(123_000, None, Config(), push_delay_seconds=60).push_at == 183_000


def test_schedule_places_quick_turn_push_after_quick_delay() -> None:
    assert schedule(123_000, None, Config(), push_delay_seconds=600).push_at == 723_000


def test_schedule_drops_push_that_would_not_precede_a_valid_warning() -> None:
    cfg = Config(warn_minutes=4, compact_minutes=4)

    assert schedule(123_000, 300, cfg, push_delay_seconds=60) == Schedule(
        push_at=None,
        warn_at=183_000,
    )
