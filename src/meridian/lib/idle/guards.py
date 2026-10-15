"""Pure ordered guards for idle stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from meridian.lib.harness.idle_types import CompactCosts, CompactEval
from meridian.lib.idle.worth import compact_worth
from meridian.lib.state.idle_store import IdleState, Stage

DecisionKind = Literal["act", "skip"]


class GuardConfig(Protocol):
    """Effective settings used by the ordered guards."""

    @property
    def compact(self) -> bool: ...

    @property
    def compact_min_growth_tokens(self) -> int | None: ...

    @property
    def late_fire_tolerance_seconds(self) -> int: ...


@dataclass(frozen=True)
class GuardFacts:
    """Timer identity, launch role, and current harness facts."""

    stretch: int
    anchor: int
    role: str | None = "primary"
    harness_enabled: bool = True
    busy: bool = False
    agents_running: int = 0
    child_spawns_active: bool = False
    context_tokens: int | None = None
    harness_autocompact_off: bool = False


@dataclass(frozen=True)
class Decision:
    decision: DecisionKind
    reason: str
    compact_eval: CompactEval | None = None

    @classmethod
    def act(cls, compact_eval: CompactEval | None = None) -> Decision:
        return cls("act", "guards-passed", compact_eval)

    @classmethod
    def skip(cls, reason: str, compact_eval: CompactEval | None = None) -> Decision:
        return cls("skip", reason, compact_eval)


def _scheduled_at(state: IdleState, stage: Stage) -> int | None:
    return getattr(state.schedule, f"{stage}_at")


def decide(
    stage: Stage,
    facts: GuardFacts,
    state: IdleState | None,
    cfg: GuardConfig,
    now_ms: int,
    *,
    compact_costs: CompactCosts | None = None,
) -> Decision:
    """Return the first matching guard, in architecture section 4.2 order."""

    if facts.role != "primary":
        return Decision.skip("not-primary")
    if not facts.harness_enabled:
        return Decision.skip("idle-disabled")
    if state is not None and stage in state.done:
        return Decision.skip("stage-done")
    if state is None or not state.stretch_open:
        return Decision.skip("stretch-closed")
    if facts.stretch != state.stretch or facts.anchor != state.anchor:
        return Decision.skip("stale-anchor")
    scheduled_at = _scheduled_at(state, stage)
    if scheduled_at is None:
        return Decision.skip("not-scheduled")
    if now_ms < scheduled_at - 1000:
        return Decision.skip("early-fire")
    if now_ms - scheduled_at > cfg.late_fire_tolerance_seconds * 1000:
        return Decision.skip("late-fire")

    if stage != "compact":
        return Decision.act()
    if not cfg.compact:
        return Decision.skip("compaction-disabled")
    if state.ttl_seconds is not None and state.idle_since_ms is not None and now_ms >= (
        state.idle_since_ms + state.ttl_seconds * 1000
    ):
        return Decision.skip("cache-cold")
    if facts.busy:
        return Decision.skip("busy")
    if facts.agents_running > 0:
        return Decision.skip("agents-running")
    if facts.child_spawns_active:
        return Decision.skip("child-spawns-running")
    evaluation = compact_worth(
        context_tokens=facts.context_tokens,
        baseline_tokens=state.context.baseline_tokens,
        baseline_source=state.context.baseline_source,
        costs=compact_costs,
        override_tokens=cfg.compact_min_growth_tokens,
    )
    if evaluation.verdict != "worth-it":
        return Decision.skip(evaluation.verdict, evaluation)
    if facts.harness_autocompact_off:
        return Decision.skip("harness-autocompact-off", evaluation)
    return Decision.act(evaluation)


__all__ = ["Decision", "DecisionKind", "GuardConfig", "GuardFacts", "decide"]
