from dataclasses import dataclass

import pytest

from meridian.lib.idle.timeline import Schedule, schedule, select_push_delay


@dataclass(frozen=True)
class Config:
    push_seconds: int = 60
    long_turn_seconds: int = 60
    quick_turn_push_seconds: int = 600
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


@pytest.mark.parametrize(
    ("now_ms", "returned_at_ms", "previous_idle_since_ms", "expected"),
    [
        (120_000, 60_000, None, 60),
        (119_999, 60_000, None, 600),
        (120_000, None, None, 600),
        (120_000, 60_000, 60_000, 600),
    ],
    ids=["long-turn", "quick-turn", "no-return", "stale-return"],
)
def test_select_push_delay_uses_only_a_fresh_return_for_turn_length(
    now_ms: int,
    returned_at_ms: int | None,
    previous_idle_since_ms: int | None,
    expected: int,
) -> None:
    assert (
        select_push_delay(
            now_ms,
            returned_at_ms,
            previous_idle_since_ms,
            Config(),
        )
        == expected
    )


def test_select_push_delay_zero_threshold_always_uses_long_turn_delay() -> None:
    assert select_push_delay(0, None, None, Config(long_turn_seconds=0)) == 60
