from dataclasses import dataclass, replace

import pytest

from meridian.lib.idle.guards import GuardFacts, decide
from meridian.lib.state.idle_store import IdleSchedule, IdleState, Stage


@dataclass(frozen=True)
class Config:
    compact: bool = True
    min_compact_tokens: int = 40_000
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
        "schedule": IdleSchedule(
            push_at=60_000,
            warn_at=2_700_000,
            compact_at=3_300_000,
        ),
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
        (
            "compact",
            GuardFacts(1, 1),
            state(),
            Config(compact=False),
            3_300_000,
            "compaction-disabled",
        ),
        (
            "compact",
            GuardFacts(1, 1),
            state(),
            Config(late_fire_tolerance_seconds=1000),
            3_600_000,
            "cache-cold",
        ),
        (
            "compact",
            GuardFacts(1, 1, busy=True),
            state(),
            Config(),
            3_300_000,
            "busy",
        ),
        (
            "compact",
            GuardFacts(1, 1, draft="yes"),
            state(),
            Config(),
            3_300_000,
            "draft-present",
        ),
        (
            "compact",
            GuardFacts(1, 1, agents_running=1),
            state(),
            Config(),
            3_300_000,
            "agents-running",
        ),
        (
            "compact",
            GuardFacts(1, 1, child_spawns_active=True),
            state(),
            Config(),
            3_300_000,
            "child-spawns-running",
        ),
        (
            "compact",
            GuardFacts(1, 1, context_tokens=39_999),
            state(),
            Config(),
            3_300_000,
            "context-too-small",
        ),
        (
            "compact",
            GuardFacts(1, 1, harness_autocompact_off=True),
            state(),
            Config(),
            3_300_000,
            "harness-autocompact-off",
        ),
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


def test_guard_order_does_not_evaluate_compaction_guards_for_push() -> None:
    facts = GuardFacts(
        1,
        1,
        draft="yes",
        busy=True,
        agents_running=1,
        child_spawns_active=True,
        context_tokens=1,
        harness_autocompact_off=True,
    )

    assert decide("push", facts, state(), Config(compact=False), 60_000).decision == "act"


def test_unknown_draft_skips_but_unknown_context_proceeds() -> None:
    unknown_draft = GuardFacts(1, 1, draft="unknown", context_tokens=None)
    safe_draft = replace(unknown_draft, draft="no")

    assert decide("compact", unknown_draft, state(), Config(), 3_300_000).reason == "draft-unknown"
    assert decide("compact", safe_draft, state(), Config(), 3_300_000).decision == "act"
