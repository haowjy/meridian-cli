from dataclasses import dataclass

import pytest

from meridian.lib.idle.timeline import Schedule, push_delay, schedule
from meridian.lib.idle.wake import ExpectedWake


@dataclass(frozen=True)
class Config:
    push_seconds: int = 60
    long_turn_seconds: int = 60
    quick_turn_push_seconds: int = 600
    warn_minutes: int = 15


@pytest.mark.parametrize(
    ("ttl_seconds", "cfg", "expected"),
    [
        (3600, Config(), Schedule(push_at=183_000, warn_at=2_823_000)),
        (300, Config(), Schedule(push_at=183_000)),
        (None, Config(), Schedule(push_at=183_000)),
        (300, Config(warn_minutes=4), Schedule(push_at=None, warn_at=183_000)),
    ],
    ids=["one-hour", "five-minutes", "unknown", "warning-must-follow-push"],
)
def test_schedule_places_only_windows_that_fit(
    ttl_seconds: int | None,
    cfg: Config,
    expected: Schedule,
) -> None:
    assert schedule(123_000, ttl_seconds, cfg, push_delay_seconds=60) == expected


@pytest.mark.parametrize(
    ("wake", "expected"),
    [
        (ExpectedWake(certain="1 agent(s) running"), Schedule(None, 2_823_000)),
        (ExpectedWake(maybe=True), Schedule(723_000, 2_823_000)),
        (ExpectedWake(certain="spawns", by_ms=3_723_000), Schedule(None, 2_823_000)),
        (ExpectedWake(certain="spawns", by_ms=3_722_999), Schedule(None, None)),
    ],
    ids=["certain-holds-push", "maybe-delays-push", "late-wake-keeps-warning", "early-wake"],
)
def test_schedule_reads_the_push_and_the_warning_from_the_expected_wake(
    wake: ExpectedWake,
    expected: Schedule,
) -> None:
    assert schedule(123_000, 3600, Config(), push_delay_seconds=60, wake=wake) == expected


@pytest.mark.parametrize(
    ("now_ms", "owed_since_ms", "cfg", "expected"),
    [
        (120_000, 60_000, Config(), 60),
        (119_999, 60_000, Config(), 600),
        (120_000, None, Config(), 600),
        (0, None, Config(long_turn_seconds=0), 60),
    ],
    ids=["long-wait", "quick-turn", "nothing-owed", "zero-threshold"],
)
def test_push_delay_follows_how_long_the_user_has_waited(
    now_ms: int,
    owed_since_ms: int | None,
    cfg: Config,
    expected: int,
) -> None:
    assert push_delay(now_ms, owed_since_ms, cfg) == expected
