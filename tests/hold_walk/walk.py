#!/usr/bin/env python3
"""Differential walker: today's `_HoldProfile` vs HoldState `decide` shell."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.lib.core.delivery import DeliveryError, DeliveryRefused, TransportDead
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.liveness import LivenessDecision
from meridian.lib.harness.semantics import (
    EventSemantics,
    NormalizedHarnessEvent,
    TerminalEventOutcome,
    TerminalOutcomeCause,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import consume_resident_signals, write_spawn_signal
from meridian.lib.streaming.completion_contracts import (
    CleanupReport,
    DiagnosticBlocker,
    EvidenceEventDecision,
    EvidenceFailure,
    ProfileDecision,
    WorkAssessment,
)
from meridian.lib.streaming.completion_coordinator import CompletionCoordinator
from meridian.lib.streaming.drain_policy import DrainAction
from tests.hold_walk.frozen_resident_drain import _HoldProfile
from tests.hold_walk.hold_profile import HoldProfile
from tests.support.fakes import FakeClock
from tests.support.launch_paths import launch_paths
from tests.support.resident_drain import FakeResidentBackendControl, start_row

TICKS = (
    0.001,
    1.0,
    4.9,
    5.0,
    5.1,
    19.0,
    20.0,
    21.0,
    59.0,
    60.0,
    61.0,
    269.0,
    270.0,
    271.0,
    299.0,
    300.0,
    301.0,
)
DESC_P2 = DiagnosticBlocker(source="persisted_descendant", code="running", identity="p2")
DESC_P3 = DiagnosticBlocker(source="persisted_descendant", code="running", identity="p3")
NATIVE = DiagnosticBlocker(source="native_task", code="running", identity="t1")
FOLLOWUP = DiagnosticBlocker(source="native_task", code="awaiting_followup_result", identity="t1")
UNREADABLE_FAIL = EvidenceFailure(code="unreadable", detail="walker")


class World:
    def __init__(self) -> None:
        self.generation = 0
        self.assessment = WorkAssessment(disposition="ready", blockers=(), generation=0)
        self.next_due: float | None = None
        self.aux = False
        self.validate_immediately = True
        self.fail_inject = False

    def set_assessment(
        self,
        disposition: str,
        blockers: tuple[DiagnosticBlocker, ...] = (),
        failure: EvidenceFailure | None = None,
    ) -> None:
        self.generation += 1
        self.assessment = WorkAssessment(
            disposition=disposition,  # type: ignore[arg-type]
            blockers=blockers,
            generation=self.generation,
            failure=failure,
        )


class ScriptedEvidence:
    def __init__(self, world: World) -> None:
        self._world = world
        self._validated: dict[int, bool] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def observe_event(self, event: object, transition: str | None) -> EvidenceEventDecision:
        del event, transition
        return EvidenceEventDecision()

    def note_event_delivered(self, event: object, state: object) -> EvidenceEventDecision:
        del event, state
        return EvidenceEventDecision()

    async def assess(self, trigger: object, state: object) -> WorkAssessment:
        del trigger, state
        return self._world.assessment

    def request_validation(self) -> int:
        req = self._world.generation
        self._validated[req] = self._world.validate_immediately
        return req

    def validation_complete(self, request: int) -> bool:
        return self._validated.get(request, False)

    def next_due_at(self) -> float | None:
        return self._world.next_due

    async def handle_due(self) -> EvidenceEventDecision:
        return EvidenceEventDecision()

    def wants_aux_wake(self) -> bool:
        return self._world.aux

    async def wait_for_change(self) -> None:
        return None


class NoopCleanup:
    async def cleanup(self, assessment: object, reason: str) -> CleanupReport:
        del assessment, reason
        return CleanupReport()


@dataclass
class Side:
    name: str
    launch: Any
    backend: FakeResidentBackendControl
    profile: Any
    coord: CompletionCoordinator
    evidence: ScriptedEvidence


def _raw() -> RawHarnessEvent:
    return RawHarnessEvent(event_type="synthetic", harness_id="codex", payload={})


def _norm(
    *,
    activity: str | None = None,
    work: bool = False,
    terminal: TerminalEventOutcome | None = None,
) -> NormalizedHarnessEvent:
    return NormalizedHarnessEvent(
        raw=_raw(),
        semantics=EventSemantics(activity=activity, work=work, terminal=terminal),  # type: ignore[arg-type]
    )


def _oc(outcome: TerminalEventOutcome | None) -> tuple | None:
    if outcome is None:
        return None
    return (outcome.status, outcome.exit_code, outcome.error, outcome.cause)


def snapshot(side: Side, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra or {}
    coord = side.coord
    backend = side.backend
    return {
        "recorded": _oc(extra.get("recorded")),
        "boundary": extra.get("boundary"),
        "nudges": tuple(backend.injected_messages),
        "deadline_at": coord.state.deadline_at,
        "next_timeout": coord.next_timeout(),
        "awaiting": (tuple(backend.awaiting_done_values), side.profile._awaiting_done),
        "defer_close": coord.should_defer_close(),
        "close_unint": _oc(coord.handle_close(intentional_stop=False)),
        "close_int": _oc(coord.handle_close(intentional_stop=True)),
        "row": getattr(side.profile, "last_row", None),
    }


_IGNORE = set(filter(None, os.environ.get("WALK_IGNORE", "").split(",")))


def diff_snap(old: dict[str, Any], new: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    out = {}
    for key in old:
        if key == "row" or key in _IGNORE:
            continue
        if old[key] != new[key]:
            out[key] = (old[key], new[key])
    return out


async def drive_event(
    side: Side,
    event: NormalizedHarnessEvent,
    *,
    terminal: TerminalEventOutcome | None = None,
    action: DrainAction | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    snaps: list[tuple[str, dict[str, Any]]] = []
    side.profile.observe_event(event.semantics)
    await side.coord.observe_event(event)
    snaps.append(("observe", snapshot(side)))
    delivered = side.coord.note_event_delivered(event.raw)
    snaps.append(("note", snapshot(side, {"recorded": delivered.recorded_outcome})))
    if delivered.recorded_outcome is not None:
        return snaps
    if terminal is not None:
        assert action is not None
        term = await side.coord.handle_terminal_event(event.raw, terminal, action)
        snaps.append(
            (
                "terminal",
                snapshot(
                    side,
                    {
                        "recorded": term.recorded_outcome,
                        "boundary": term.emit_turn_boundary,
                    },
                ),
            )
        )
        if term.recorded_outcome is not None:
            return snaps
    after = await side.coord.after_event()
    snaps.append(("after", snapshot(side, {"recorded": after.recorded_outcome})))
    return snaps


async def apply_kind(
    kind: str,
    arg: Any,
    *,
    old: Side,
    new: Side,
    world: World,
    clock: FakeClock,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Apply one scripted kind. Returns paired snapshots (label, old, new)."""
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def both_events(event, terminal=None, action=None):
        async def run():
            o = await drive_event(old, event, terminal=terminal, action=action)
            n = await drive_event(new, event, terminal=terminal, action=action)
            for i in range(max(len(o), len(n))):
                if i >= len(o) or i >= len(n):
                    lo, so = o[i] if i < len(o) else ("missing", {})
                    ln, sn = n[i] if i < len(n) else ("missing", {})
                    # Pad so diff_snap sees a mismatch rather than crashing.
                    so = so or {"recorded": ("missing",)}
                    sn = sn or {"recorded": ("missing",)}
                    pairs.append((lo if i < len(o) else ln, so, sn))
                    break
                lo, so = o[i]
                ln, sn = n[i]
                pairs.append((lo, so, sn))

        return run()

    if kind == "tick":
        clock.advance(float(arg))
        pairs.append(("tick", snapshot(old), snapshot(new)))
        return pairs
    if kind == "turn_active":
        await both_events(_norm(activity="turn_active"))
        return pairs
    if kind == "work_in":
        await both_events(_norm(work=True))
        return pairs
    if kind == "work_out":
        await both_events(_norm(work=True))
        return pairs
    if kind == "answer_turn":
        await both_events(_norm(activity="turn_active", work=True))
        return pairs
    if kind in {"success_term", "success_nonterm", "fail_term", "fail_nonterm", "replaceable"}:
        if kind.startswith("success"):
            outcome = TerminalEventOutcome(status="succeeded", exit_code=0)
        elif kind == "replaceable":
            outcome = TerminalEventOutcome(
                status="failed",
                exit_code=1,
                error="connection_closed",
                cause=TerminalOutcomeCause.REPLACEABLE_TRANSPORT_CLOSE,
            )
        else:
            outcome = TerminalEventOutcome(status="failed", exit_code=1, error="boom")
        terminate = kind in {"success_term", "fail_term", "replaceable"}
        action = DrainAction(terminate=terminate, emit_turn_boundary=not terminate)
        await both_events(_norm(terminal=outcome), terminal=outcome, action=action)
        return pairs
    if kind == "done":
        write_spawn_signal(old.launch, "done")
        write_spawn_signal(new.launch, "done")
        pairs.append(("done", snapshot(old), snapshot(new)))
        return pairs
    if kind == "rearm":
        write_spawn_signal(old.launch, "rearm")
        write_spawn_signal(new.launch, "rearm")
        pairs.append(("rearm", snapshot(old), snapshot(new)))
        return pairs
    if kind == "timeout":
        o = await old.coord.handle_timeout()
        n = await new.coord.handle_timeout()
        pairs.append(
            (
                "timeout",
                snapshot(old, {"recorded": o.recorded_outcome}),
                snapshot(new, {"recorded": n.recorded_outcome}),
            )
        )
        return pairs
    if kind == "after":
        o = await old.coord.after_event()
        n = await new.coord.after_event()
        pairs.append(
            (
                "after",
                snapshot(old, {"recorded": o.recorded_outcome}),
                snapshot(new, {"recorded": n.recorded_outcome}),
            )
        )
        return pairs
    if kind == "aux":
        world.aux = True
        o = await old.coord.handle_aux_wake()
        n = await new.coord.handle_aux_wake()
        pairs.append(
            (
                "aux",
                snapshot(old, {"recorded": o.recorded_outcome}),
                snapshot(new, {"recorded": n.recorded_outcome}),
            )
        )
        return pairs
    if kind == "block_desc":
        world.set_assessment("blocked", (DESC_P2,))
        pairs.append(("block_desc", snapshot(old), snapshot(new)))
        return pairs
    if kind == "block_native":
        world.set_assessment("blocked", (NATIVE,))
        pairs.append(("block_native", snapshot(old), snapshot(new)))
        return pairs
    if kind == "block_followup":
        world.set_assessment("blocked", (FOLLOWUP,))
        pairs.append(("block_followup", snapshot(old), snapshot(new)))
        return pairs
    if kind == "block_both":
        world.set_assessment("blocked", (DESC_P2, NATIVE))
        pairs.append(("block_both", snapshot(old), snapshot(new)))
        return pairs
    if kind == "block_change":
        world.set_assessment("blocked", (DESC_P3,))
        pairs.append(("block_change", snapshot(old), snapshot(new)))
        return pairs
    if kind == "unknown":
        world.set_assessment("unknown", (), UNREADABLE_FAIL)
        pairs.append(("unknown", snapshot(old), snapshot(new)))
        return pairs
    if kind == "ready":
        world.set_assessment("ready")
        pairs.append(("ready", snapshot(old), snapshot(new)))
        return pairs
    if kind == "dead":
        old.backend.status = LivenessDecision.BACKEND_DEAD
        new.backend.status = LivenessDecision.BACKEND_DEAD
        pairs.append(("dead", snapshot(old), snapshot(new)))
        return pairs
    if kind == "live":
        old.backend.status = LivenessDecision.CONTINUE
        new.backend.status = LivenessDecision.CONTINUE
        pairs.append(("live", snapshot(old), snapshot(new)))
        return pairs
    if kind == "refuse":
        old.backend.inject_error = DeliveryRefused("no")
        new.backend.inject_error = DeliveryRefused("no")
        pairs.append(("refuse", snapshot(old), snapshot(new)))
        return pairs
    if kind in {"gone", "may_arrive"}:
        make = (lambda: TransportDead("gone")) if kind == "gone" else (lambda: DeliveryError("?"))
        old.backend.inject_error = make()
        new.backend.inject_error = make()
        if kind == "gone":  # PR 1: TransportDead implies health says dead, bar a send race
            old.backend.status = LivenessDecision.BACKEND_DEAD
            new.backend.status = LivenessDecision.BACKEND_DEAD
        pairs.append((kind, snapshot(old), snapshot(new)))
        return pairs
    if kind == "accept":
        old.backend.inject_error = None
        new.backend.inject_error = None
        pairs.append(("accept", snapshot(old), snapshot(new)))
        return pairs
    if kind == "close":
        pairs.append(("close", snapshot(old), snapshot(new)))
        return pairs
    if kind == "validate_later":
        world.validate_immediately = False
        pairs.append(("validate_later", snapshot(old), snapshot(new)))
        return pairs
    if kind == "validate_now":
        world.validate_immediately = True
        old.evidence._validated = {k: True for k in old.evidence._validated}
        new.evidence._validated = {k: True for k in new.evidence._validated}
        pairs.append(("validate_now", snapshot(old), snapshot(new)))
        return pairs
    raise ValueError(kind)


class _FrozenWithDeltas(_HoldProfile):
    """The frozen hold plus each stated delta (WALK_DELTAS=1,2,3,4), to see past them."""

    DELTAS = frozenset(filter(None, os.environ.get("WALK_DELTAS", "").split(",")))

    def _evaluate_terminal(self, context):
        outcome = context.terminal_outcome
        action = context.terminal_action
        success_end = action.terminate and outcome.status == "succeeded"
        if (
            "1" in self.DELTAS
            and success_end
            and self._idle_nudge_outstanding
            and self._resident_health_status() == LivenessDecision.BACKEND_DEAD
        ):
            self.clear()
            return self._window.timeout_decision()
        if "4" in self.DELTAS and action.terminate and self._grace_passed(context.now):
            self._confirmation_grace_at = None
            self._confirmation_failed = True
        decision = super()._evaluate_terminal(context)
        if (
            "3" in self.DELTAS
            and success_end
            and decision.action == "complete"
            and not context.directives.done
            and self._accepted_signals
            and self._confirmation_grace_at is not None
            and not self._confirmation_failed
            and not self._confirmation_turn_started
        ):
            return ProfileDecision(action="wait", emit_turn_boundary=True)
        return decision

    def _grace_passed(self, now):
        return (
            self._confirmation_grace_at is not None
            and now >= self._confirmation_grace_at
            and not self._confirmation_turn_started
        )

    async def send_nudge(self, state):
        expiry_nudge = self._idle_nudge_outstanding
        try:
            await super().send_nudge(state)
        except DeliveryError as exc:
            unknown = type(exc) is DeliveryError
            if "2" in self.DELTAS and unknown:
                # Delivered as far as the hold can tell: the grace runs.
                self._confirmation_failed = False
                self._confirmation_grace_at = self._clock() + self._confirmation_grace_seconds
                if expiry_nudge:
                    self._confirmation_turn_started = False
                return
            if "4" in self.DELTAS:
                self._confirmation_grace_at = None
            raise
        except RuntimeError:
            if "4" in self.DELTAS:
                self._confirmation_grace_at = None
            raise


_FROZEN = _FrozenWithDeltas if os.environ.get("WALK_DELTAS") else _HoldProfile


@dataclass
class WalkConfig:
    deadline_seconds: float
    poll_seconds: float
    signals: bool
    rearm_budget: int | None
    backend_none: bool = False  # Claude's path: no backend control, health decides


def ensure_row(tmp: Path, spawn_id: str) -> None:
    if spawn_store.get_spawn(tmp, SpawnId(spawn_id)) is None:
        start_row(tmp, spawn_id, HarnessId.CODEX, None)


def build_side(
    *,
    tmp: Path,
    spawn_id: str,
    clock: FakeClock,
    world: World,
    cfg: WalkConfig,
    profile_cls: type,
) -> Side:
    ensure_row(tmp, spawn_id)
    launch = launch_paths(tmp, SpawnId(spawn_id))
    backend = FakeResidentBackendControl()
    evidence = ScriptedEvidence(world)
    profile = profile_cls(
        launch=launch,
        backend=None if cfg.backend_none else backend,
        health=lambda: backend.status != LivenessDecision.BACKEND_DEAD,
        send_nudge=backend.begin_followup_turn,
        deadline_seconds=cfg.deadline_seconds,
        poll_seconds=cfg.poll_seconds,
        rearm_budget=cfg.rearm_budget,
        clock=clock.monotonic,
        completion_signals_available=cfg.signals,
    )
    coord = CompletionCoordinator(
        evidence=evidence,
        profile=profile,
        cleanup=NoopCleanup(),
        clock=clock.monotonic,
    )
    return Side(
        name=spawn_id,
        launch=launch,
        backend=backend,
        profile=profile,
        coord=coord,
        evidence=evidence,
    )


def reset_spawn(tmp: Path, spawn_id: str) -> None:
    launch = launch_paths(tmp, SpawnId(spawn_id))
    consume_resident_signals(launch)
    spawn_store.update_spawn(tmp, SpawnId(spawn_id), resident_rearm_count=0)


KIND_WEIGHTS: list[tuple[str, int]] = [
    ("tick", 10),
    ("turn_active", 6),
    ("work_in", 6),
    ("work_out", 2),
    ("answer_turn", 4),
    ("success_term", 8),
    ("success_nonterm", 2),
    ("fail_term", 3),
    ("fail_nonterm", 1),
    ("replaceable", 3),
    ("done", 5),
    ("rearm", 5),
    ("timeout", 8),
    ("after", 5),
    ("aux", 2),
    ("block_desc", 4),
    ("block_native", 3),
    ("block_followup", 2),
    ("block_both", 2),
    ("block_change", 2),
    ("unknown", 4),
    ("ready", 4),
    ("dead", 3),
    ("live", 2),
    ("refuse", 4),
    ("gone", 2),
    ("may_arrive", 3),
    ("accept", 3),
    ("close", 2),
    ("validate_later", 1),
    ("validate_now", 1),
]
_WITHOUT = set(filter(None, os.environ.get("WALK_WITHOUT", "").split(",")))
_KIND_BAG = [k for k, w in KIND_WEIGHTS for _ in range(w) if k not in _WITHOUT]


def random_script(rng: random.Random, n: int) -> list[tuple[str, Any]]:
    steps: list[tuple[str, Any]] = []
    for _ in range(n):
        kind = rng.choice(_KIND_BAG)
        arg = rng.choice(TICKS) if kind == "tick" else None
        steps.append((kind, arg))
    return steps


FORCED: list[list[tuple[str, Any]]] = [
    # A1: non-terminating action after a hold
    [("success_term", None), ("fail_nonterm", None)],
    # A5: failed terminate
    [("success_term", None), ("fail_term", None)],
    # A2 ready replaceable
    [("success_term", None), ("timeout", None), ("replaceable", None)],
    # A4 unknown replaceable
    [("unknown", None), ("success_term", None), ("replaceable", None)],
    # A3 expired blocked replaceable
    [
        ("block_desc", None),
        ("success_term", None),
        ("tick", 21.0),
        ("replaceable", None),
    ],
    # B2 rearm
    [("success_term", None), ("rearm", None), ("timeout", None)],
    # C1/C2 after rearm
    [("success_term", None), ("rearm", None), ("timeout", None), ("tick", 21.0), ("timeout", None)],
    # D5 done publish
    [("success_term", None), ("done", None), ("timeout", None)],
    # D2 unknown done
    [("unknown", None), ("success_term", None), ("done", None), ("timeout", None)],
    # D1 unknown done expired
    [
        ("unknown", None),
        ("success_term", None),
        ("done", None),
        ("tick", 21.0),
        ("timeout", None),
    ],
    # D3/D4 closing + active turn
    [
        ("success_term", None),
        ("done", None),
        ("turn_active", None),
        ("timeout", None),
        ("tick", 21.0),
        ("timeout", None),
    ],
    # E5 ask then grace
    [("success_term", None), ("tick", 5.1), ("timeout", None)],
    # E6 after answering
    [
        ("success_term", None),
        ("answer_turn", None),
        ("work_in", None),
        ("success_term", None),
    ],
    # E3 active turn hold
    [("success_term", None), ("turn_active", None), ("after", None)],
    # E4 unknown hold
    [("unknown", None), ("success_term", None), ("after", None)],
    # E1 expiry ask
    [("success_term", None), ("tick", 21.0), ("timeout", None)],
    # refused turn-end ask
    [("refuse", None), ("success_term", None)],
    # refused then accept expiry
    [
        ("success_term", None),
        ("tick", 21.0),
        ("refuse", None),
        ("timeout", None),
        ("accept", None),
        ("tick", 301.0),
        ("timeout", None),
    ],
    # B1 dead during answer window
    [
        ("success_term", None),
        ("tick", 21.0),
        ("timeout", None),
        ("dead", None),
        ("timeout", None),
    ],
    # delta 1: dead + rearm at turn end with answer open
    [
        ("success_term", None),
        ("tick", 21.0),
        ("timeout", None),
        ("dead", None),
        ("rearm", None),
        ("success_term", None),
    ],
    # lease held: rearm with blockers, success again
    [
        ("block_desc", None),
        ("success_term", None),
        ("rearm", None),
        ("timeout", None),
        ("success_term", None),
        ("after", None),
    ],
    # lease drop then ask
    [
        ("block_desc", None),
        ("success_term", None),
        ("rearm", None),
        ("timeout", None),
        ("ready", None),
        ("success_term", None),
    ],
    # spaced lease 270
    [
        ("success_term", None),
        ("rearm", None),
        ("timeout", None),
        ("success_term", None),
        ("tick", 269.0),
        ("after", None),
        ("tick", 2.0),
        ("after", None),
    ],
    # followup-result still holds
    [
        ("block_desc", None),
        ("success_term", None),
        ("rearm", None),
        ("timeout", None),
        ("block_followup", None),
        ("success_term", None),
    ],
    # no-signal hold
    [("success_term", None), ("tick", 1.0), ("after", None), ("tick", 1.0), ("timeout", None)],
    # backend dead on send
    [("dead", None), ("success_term", None)],
    # close while blocked descendant
    [("block_desc", None), ("success_term", None), ("close", None)],
    # close while rearmed
    [("success_term", None), ("rearm", None), ("timeout", None), ("close", None)],
    # work out of turn does not close answer
    [
        ("success_term", None),
        ("tick", 21.0),
        ("timeout", None),
        ("work_out", None),
        ("tick", 301.0),
        ("timeout", None),
    ],
]


async def run_script(
    steps: list[tuple[str, Any]],
    *,
    tmp: Path,
    cfg: WalkConfig,
    clock_start: float = 100.0,
) -> tuple[list[str], dict[str, tuple[Any, Any]] | None, list[str], int]:
    """Returns (labels, first_diff or None, rows_hit, steps_compared)."""
    clock = FakeClock(start=clock_start)
    world = World()
    reset_spawn(tmp, "pold")
    reset_spawn(tmp, "pnew")
    old = build_side(
        tmp=tmp,
        spawn_id="pold",
        clock=clock,
        world=world,
        cfg=cfg,
        profile_cls=_FROZEN,
    )
    new = build_side(
        tmp=tmp,
        spawn_id="pnew",
        clock=clock,
        world=world,
        cfg=cfg,
        profile_cls=HoldProfile,
    )
    rows: list[str] = []
    compared = 0
    for kind, arg in steps:
        pairs = await apply_kind(kind, arg, old=old, new=new, world=world, clock=clock)
        for label, so, sn in pairs:
            compared += 1
            row = sn.get("row")
            if row:
                rows.append(str(row))
            d = diff_snap(so, sn)
            if d:
                return rows, d, [f"{kind}:{arg}", label], compared
    return rows, None, [], compared


def minimize(
    steps: list[tuple[str, Any]],
    *,
    tmp: Path,
    cfg: WalkConfig,
) -> tuple[list[tuple[str, Any]], dict[str, tuple[Any, Any]] | None]:
    current = list(steps)
    changed = True
    last_diff: dict[str, tuple[Any, Any]] | None = None
    while changed:
        changed = False
        i = 0
        while i < len(current):
            trial = current[:i] + current[i + 1 :]
            rows, diff, _, _ = asyncio.get_event_loop().run_until_complete(
                run_script(trial, tmp=tmp, cfg=cfg)
            )
            del rows
            if diff is not None:
                current = trial
                last_diff = diff
                changed = True
            else:
                i += 1
    if last_diff is None:
        _, last_diff, _, _ = asyncio.get_event_loop().run_until_complete(
            run_script(current, tmp=tmp, cfg=cfg)
        )
    return current, last_diff


def _looks_like_delta1(diff, steps):
    """Old side took the rearm (no outcome, abandon), new side timed out at a terminal."""
    rec = diff.get("recorded")
    if not rec or rec[0] is not None or rec[1] is None:
        return False
    if rec[1][2] not in {"resident_deadline_expired", "resident_rearm_budget_exhausted"}:
        return False
    kinds = [k for k, _ in steps]
    return "rearm" in kinds and "dead" in kinds


def random_cfg(rng: random.Random) -> WalkConfig:
    return WalkConfig(
        deadline_seconds=rng.choice((20.0, 60.0)),
        poll_seconds=rng.choice((5.0, 5.0, 5.0, 1.0)),
        signals=rng.choice((True, True, True, False)),
        rearm_budget=rng.choice((None, None, 1)),
        backend_none=rng.choice((False, False, True)),
    )


async def main_async(args: argparse.Namespace) -> int:
    logging.getLogger("meridian.lib.streaming.completion_coordinator").setLevel(logging.CRITICAL)
    # Durability is irrelevant to profile decisions; fsync dominates wall time.
    import posix

    os.fsync = lambda _fd: None  # type: ignore[assignment]
    posix.fsync = lambda _fd: None  # type: ignore[assignment]
    tmp = Path(os.environ.get("WALK_RT", "/tmp/p10702/walk-rt"))
    tmp.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    # Ensure spawn rows exist once.
    clock = FakeClock(start=100.0)
    world = World()
    cfg0 = WalkConfig(20.0, 5.0, True, None)
    build_side(
        tmp=tmp, spawn_id="pold", clock=clock, world=world, cfg=cfg0, profile_cls=_HoldProfile
    )
    build_side(
        tmp=tmp, spawn_id="pnew", clock=clock, world=world, cfg=cfg0, profile_cls=HoldProfile
    )

    rows_hit: set[str] = set()
    divergences: list[dict[str, Any]] = []
    fingerprints: set[tuple] = set()
    walks = 0
    compared_total = 0
    clean = 0
    classified = 0

    scripts: list[tuple[WalkConfig, list[tuple[str, Any]]]] = []
    for forced in FORCED:
        scripts.append((WalkConfig(20.0, 5.0, True, None), forced))
        scripts.append((WalkConfig(60.0, 5.0, True, None), forced))
        scripts.append((WalkConfig(20.0, 5.0, False, None), forced))
        scripts.append((WalkConfig(20.0, 5.0, True, None, True), forced))

    target = args.n
    while len(scripts) < target:
        cfg = random_cfg(rng)
        nsteps = rng.randint(4, 16)
        scripts.append((cfg, random_script(rng, nsteps)))

    for cfg, steps in scripts[:target]:
        walks += 1
        try:
            rows, diff, where, compared = await run_script(steps, tmp=tmp, cfg=cfg)
        except Exception as exc:
            diff = {"exception": (type(exc).__name__, traceback.format_exc())}
            rows, where, compared = [], ["raise"], 0
        rows_hit.update(rows)
        compared_total += compared
        if diff is None:
            clean += 1
        elif _looks_like_delta1(diff, steps):
            classified += 1
            if "delta1" not in fingerprints:
                fingerprints.add("delta1")
                divergences.append(
                    {
                        "walk": walks,
                        "cfg": cfg,
                        "where": where,
                        "diff": diff,
                        "script": steps,
                        "full_len": len(steps),
                        "class": "delta1",
                    }
                )
                print(f"CLASSIFIED delta1 walk={walks} script={steps[:12]}...", flush=True)
        else:
            current = list(steps)
            last = diff
            changed = True
            try:
                while changed:
                    changed = False
                    i = 0
                    while i < len(current):
                        trial = current[:i] + current[i + 1 :]
                        _, d, _, _ = await run_script(trial, tmp=tmp, cfg=cfg)
                        if d is not None:
                            current = trial
                            last = d
                            changed = True
                        else:
                            i += 1
            except Exception:
                current, last = steps, diff
            mini, mini_diff = current, last
            # Coarser fingerprint: kind sequence + differing keys.
            coarse = (tuple(k for k, _ in mini), tuple(sorted((mini_diff or diff).keys())))
            if coarse not in fingerprints:
                fingerprints.add(coarse)
                divergences.append(
                    {
                        "walk": walks,
                        "cfg": cfg,
                        "where": where,
                        "diff": mini_diff or diff,
                        "script": mini,
                        "full_len": len(steps),
                    }
                )
                print(
                    f"DIVERGENCE walk={walks} keys={list((mini_diff or diff).keys())} "
                    f"script={mini} cfg={cfg}",
                    flush=True,
                )
                if args.stop_after and len(divergences) >= args.stop_after:
                    break
        if walks % 500 == 0:
            print(
                f"progress walks={walks} clean={clean} classified={classified} "
                f"unique_div={len(divergences)} rows={sorted(rows_hit)}",
                flush=True,
            )

    print("--- summary ---")
    print(
        f"walks={walks} clean={clean} classified={classified} "
        f"unique_div={len(divergences)} compared={compared_total}"
    )
    print(f"rows_hit={sorted(rows_hit)}")
    expected = {
        *(f"A{n}" for n in range(1, 6)),
        "B1",
        "B2",
        "C1",
        "C2",
        "D1",
        "D2",
        "D3",
        *(f"E{n}" for n in range(1, 7)),
    }
    missed = sorted(expected - rows_hit)
    print(f"rows_missed={missed}")
    out = Path(os.environ.get("WALK_OUT", "/tmp/p10702/div.txt"))
    lines = [f"walks={walks} clean={clean} unique={len(divergences)} compared={compared_total}"]
    for i, d in enumerate(divergences, 1):
        lines.append(f"\n# {i} walk={d['walk']} cfg={d['cfg']}")
        lines.append(f"keys={d['diff']}")
        lines.append(f"script={d['script']}")
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")
    return 0 if not divergences else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=30000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--stop-after", type=int, default=0)
    args = p.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
