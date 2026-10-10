"""Pure ordered guards for idle stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from meridian.lib.state.idle_store import IdleState, Stage

DecisionKind = Literal["act", "skip"]


class GuardConfig(Protocol):
    """Effective settings used by the ordered guards."""

    @property
    def compact(self) -> bool: ...

    @property
    def min_compact_tokens(self) -> int: ...

    @property
    def late_fire_tolerance_seconds(self) -> int: ...


@dataclass(frozen=True)
class GuardFacts:
    """Timer identity, launch role, and current harness facts."""

    stretch: int
    anchor: int
    role: str | None = "primary"
    harness_enabled: bool = True
    draft: Literal["yes", "no", "unknown"] = "no"
    busy: bool = False
    agents_running: int = 0
    child_spawns_active: bool = False
    context_tokens: int | None = None
    harness_autocompact_off: bool = False


@dataclass(frozen=True)
class Decision:
    decision: DecisionKind
    reason: str

    @classmethod
    def act(cls) -> Decision:
        return cls("act", "guards-passed")

    @classmethod
    def skip(cls, reason: str) -> Decision:
        return cls("skip", reason)


def _scheduled_at(state: IdleState, stage: Stage) -> int | None:
    return getattr(state.schedule, f"{stage}_at")


def decide(
    stage: Stage,
    facts: GuardFacts,
    state: IdleState | None,
    cfg: GuardConfig,
    now_ms: int,
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
    if facts.draft != "no":
        return Decision.skip("draft-unknown" if facts.draft == "unknown" else "draft-present")
    if facts.agents_running > 0:
        return Decision.skip("agents-running")
    if facts.child_spawns_active:
        return Decision.skip("child-spawns-running")
    if facts.context_tokens is not None and facts.context_tokens < cfg.min_compact_tokens:
        return Decision.skip("context-too-small")
    if facts.harness_autocompact_off:
        return Decision.skip("harness-autocompact-off")
    return Decision.act()


__all__ = ["Decision", "DecisionKind", "GuardConfig", "GuardFacts", "decide"]
