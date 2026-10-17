from dataclasses import dataclass

import pytest

from meridian.lib.idle.guards import GuardFacts, decide
from meridian.lib.state.idle_store import IdleSchedule, IdleState, Stage


@dataclass(frozen=True)
class Config:
    late_fire_tolerance_seconds: int = 120


def state(**updates: object) -> IdleState:
    base: dict[str, object] = {
        "harness": "test",
        "session": "s1",
        "stretch": 1,
        "stretch_open": True,
        "anchor": 1,
        "idle_since_ms": 0,
        "ttl_seconds": 3600,
        "schedule": IdleSchedule(push_at=60_000, warn_at=2_700_000),
        "updated_at_ms": 0,
    }
    base.update(updates)
    return IdleState.model_validate(base)


@pytest.mark.parametrize(
    ("stage", "facts", "current", "cfg", "now_ms", "reason"),
    [
        (
            "push",
            GuardFacts(1, 2, role="spawn", harness_enabled=False),
            state(done={"push": "sent"}, stretch_open=False),
            Config(),
            0,
            "not-primary",
        ),
        (
            "push",
            GuardFacts(1, 1, harness_enabled=False),
            state(),
            Config(),
            60_000,
            "idle-disabled",
        ),
        (
            "push",
            GuardFacts(1, 1),
            state(done={"push": "sent"}),
            Config(),
            60_000,
            "stage-done",
        ),
        (
            "push",
            GuardFacts(1, 1),
            state(stretch_open=False),
            Config(),
            60_000,
            "stretch-closed",
        ),
        ("push", GuardFacts(1, 2), state(), Config(), 60_000, "stale-anchor"),
        ("push", GuardFacts(1, 1), state(), Config(), 58_999, "early-fire"),
        ("push", GuardFacts(1, 1), state(), Config(), 181_001, "late-fire"),
    ],
)
def test_guard_order_first_match(
    stage: Stage,
    facts: GuardFacts,
    current: IdleState,
    cfg: Config,
    now_ms: int,
    reason: str,
) -> None:
    assert decide(stage, facts, current, cfg, now_ms).reason == reason

