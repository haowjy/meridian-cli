from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TypeVar

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.harness.idle_types import DetectIdleTtl, IdleFacts
from meridian.lib.idle.service import (
    AutocompactOffReader,
    ConfigResult,
    IdleService,
    effective,
)
from meridian.lib.notify import Notice, SendReport
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.state.idle_store import IdleState

T = TypeVar("T")


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def now_ms(self) -> int:
        return self.value


class MemoryStore:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], IdleState] = {}
        self.lock = Lock()

    def read(self, harness: str, session: str) -> IdleState | None:
        return self.states.get((harness, session))

    def mutate(
        self,
        harness: str,
        session: str,
        mutation: Callable[[IdleState | None], tuple[IdleState | None, T]],
    ) -> T:
        with self.lock:
            key = (harness, session)
            state, result = mutation(self.states.get(key))
            if state is not None:
                self.states[key] = state
            return result

    def list_states(self) -> tuple[IdleState, ...]:
        return tuple(self.states.values())


class Sender:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.notices: list[Notice] = []

    def __call__(self, notice: Notice, cfg: object) -> SendReport:
        _ = cfg
        self.notices.append(notice)
        status = "sent" if self.ok else "failed"
        return SendReport(results=(SendResult(channel="test", status=status),))


SAFE_FACTS = IdleFacts(
    draft="no",
    busy=False,
    agents_running=0,
    context_tokens=100_000,
    harness_autocompact_off=False,
)


def service(
    *,
    clock: Clock | None = None,
    store: MemoryStore | None = None,
    sender: Sender | None = None,
    config: MeridianConfig | None = None,
    env: dict[str, str] | None = None,
    interactive: bool = False,
    autocompact_off: AutocompactOffReader | None = None,
    detect_ttl: DetectIdleTtl | None = None,
) -> tuple[IdleService, Clock, MemoryStore, Sender]:
    resolved_clock = clock or Clock()
    resolved_store = store or MemoryStore()
    resolved_sender = sender or Sender()
    resolved_env = {"MERIDIAN_SESSION_ROLE": "primary", **(env or {})}
    return (
        IdleService(
            store=resolved_store,
            config=config or MeridianConfig(),
            env=resolved_env,
            now_ms=resolved_clock.now_ms,
            notify_sender=resolved_sender,
            spawn_reader=lambda: (),
            interactive=interactive,
            autocompact_off=autocompact_off,
            detect_ttl=detect_ttl,
        ),
        resolved_clock,
        resolved_store,
        resolved_sender,
    )


def test_interactive_role_resolution_only_promotes_an_unset_role() -> None:
    def config(*, env: dict[str, str], interactive: bool) -> ConfigResult:
        return IdleService(
            store=MemoryStore(),
            config=MeridianConfig(),
            env=env,
            notify_sender=Sender(),
            spawn_reader=lambda: (),
            interactive=interactive,
        ).config_for("example")

    assert config(env={}, interactive=True).enabled is True
    assert config(env={}, interactive=False).reason == "interactive"
    assert config(env={"MERIDIAN_SESSION_ROLE": "spawn"}, interactive=True).reason == "role"


def test_effective_env_level_beats_more_specific_file_in_both_directions() -> None:
    specific_true = MeridianConfig.model_validate(
        {
            "harness": {"codex": {"idle": {"compact": True}}},
            "idle": {"compact": False},
        }
    )
    specific_false = MeridianConfig.model_validate(
        {
            "harness": {"codex": {"idle": {"compact": False}}},
            "idle": {"compact": True},
        }
    )

    assert effective("compact", "codex", specific_true, {"MERIDIAN_IDLE_COMPACT": "0"}) is False
    assert effective("compact", "codex", specific_false, {"MERIDIAN_IDLE_COMPACT": "1"}) is True
    assert effective("compact", "codex", specific_true, {}) is True


def test_config_role_and_enabled_gates_do_not_write() -> None:
    disabled, _, disabled_store, _ = service(
        config=MeridianConfig.model_validate({"idle": {"enabled": False}})
    )
    spawn, _, spawn_store, _ = service(env={"MERIDIAN_SESSION_ROLE": "spawn"})

    assert disabled.arm(harness="example", session="s1").reason == "idle-disabled"
    assert spawn.arm(harness="example", session="s1").reason == "role"
    assert disabled_store.states == {}
    assert spawn_store.states == {}


def test_arm_resolves_explicit_configured_then_detected_ttl() -> None:
    detected: list[str] = []

    def detect_ttl(**kwargs: object) -> int:
        detected.append(str(kwargs["session_id"]))
        return 300

    idle, _, store, _ = service(detect_ttl=detect_ttl)
    idle.arm(harness="example", session="explicit", ttl_seconds=600)
    idle.arm(harness="example", session="detected")

    configured, _, configured_store, _ = service(
        config=MeridianConfig.model_validate(
            {"harness": {"codex": {"idle": {"ttl_seconds": 900}}}}
        ),
        detect_ttl=detect_ttl,
    )
    configured.arm(harness="codex", session="configured")

    assert detected == ["detected"]
    assert store.read("example", "explicit").ttl_seconds == 600  # type: ignore[union-attr]
    assert store.read("example", "detected").ttl_seconds == 300  # type: ignore[union-attr]
    assert configured_store.read("codex", "configured").ttl_seconds == 900  # type: ignore[union-attr]


def test_return_then_long_turn_uses_the_short_push_delay() -> None:
    idle, clock, store, _ = service()
    clock.value = 1_000
    idle.return_(harness="example", session="s1", user_prompt=True)
    clock.value += 120_000

    armed = idle.arm(harness="example", session="s1")

    assert armed.push_at == clock.value + 60_000
    assert store.read("example", "s1").returned_at_ms == 1_000  # type: ignore[union-attr]


def test_arm_without_a_recorded_return_uses_the_quick_turn_delay() -> None:
    idle, clock, _, _ = service()

    armed = idle.arm(harness="example", session="s1")

    assert armed.push_at == clock.value + 600_000


def test_zero_long_turn_threshold_always_uses_the_short_push_delay() -> None:
    config = MeridianConfig.model_validate({"idle": {"long_turn_seconds": 0}})
    idle, clock, _, _ = service(config=config)

    armed = idle.arm(harness="example", session="s1")

    assert armed.push_at == clock.value + 60_000


def test_arm_opens_reanchors_and_never_reenables_a_done_stage() -> None:
    idle, clock, store, sender = service()
    first = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = first.push_at or 0
    fired = idle.fire(
        "push",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
    )
    clock.value += 1000
    second = idle.arm(harness="example", session="s1", ttl_seconds=3600)

    assert fired.decision == "act"
    assert sender.notices[0].body == "waiting on you"
    assert (second.stretch, second.anchor, second.push_at) == (1, 2, None)
    assert store.read("example", "s1").done == {"push": "sent"}  # type: ignore[union-attr]


def test_failed_notification_is_final_and_not_retried() -> None:
    sender = Sender(ok=False)
    idle, clock, store, _ = service(sender=sender)
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.push_at or 0

    first = idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)
    second = idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)
    assert first.decision == "act"
    assert second.reason == "stage-done"
    assert store.read("example", "s1").done["push"] == "failed"  # type: ignore[union-attr]
    assert len(sender.notices) == 1


def test_stale_anchor_fire_does_not_claim_the_new_anchor_stage() -> None:
    idle, clock, store, _ = service()
    idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = 1
    current = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = current.push_at or 0

    result = idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)

    assert result.reason == "stale-anchor"
    assert store.read("example", "s1").done == {}  # type: ignore[union-attr]


def test_compaction_claim_window_done_ok_and_compacted_state() -> None:
    idle, clock, store, sender = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0

    claim = idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )
    ignored = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    done = idle.done(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        result="ok",
        detail="100k → summary",
    )
    clock.value += 31_000
    still_compacted = idle.arm(harness="example", session="s1", ttl_seconds=3600)

    current = store.read("example", "s1")
    assert claim.decision == "act"
    assert ignored.reason == "compact-window"
    assert (ignored.push_at, ignored.warn_at, ignored.compact_at) == (None, None, None)
    assert done.recorded is True
    assert still_compacted.reason == "already-compacted"
    assert current is not None and current.done["compact"] == "ok"
    assert sender.notices[-1].body == "compacted (100k → summary)"


def test_closed_compacted_stretch_opens_a_new_stretch_on_arm() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )
    idle.done("compact", harness="example", session="s1", stretch=1, result="ok")

    closed = idle.return_(harness="example", session="s1", user_prompt=True)
    opened = idle.arm(harness="example", session="s1", ttl_seconds=3600)

    assert closed.stretch_closed == 1
    assert (opened.stretch, opened.anchor, opened.reason) == (2, 1, None)
    current = store.read("example", "s1")
    assert current is not None
    assert current.stretch_open is True
    assert current.done == {}


def test_implies_return_opens_new_stretch_inside_claimed_window() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )

    opened = idle.arm(
        harness="example",
        session="s1",
        ttl_seconds=3600,
        implies_return=True,
        turn_id="user-turn-2",
    )
    duplicate = idle.arm(
        harness="example",
        session="s1",
        ttl_seconds=3600,
        implies_return=True,
        turn_id="user-turn-2",
    )

    assert (opened.stretch, opened.anchor, opened.reason) == (2, 1, None)
    assert (duplicate.stretch, duplicate.anchor, duplicate.reason) == (
        2,
        1,
        "duplicate-turn",
    )
    current = store.read("example", "s1")
    assert current is not None
    assert current.done == {}
    assert current.compact_window_until_ms is None


def test_harness_autocompact_env_fact_is_merged_before_compaction_guard() -> None:
    idle, clock, store, _ = service(autocompact_off=lambda env: True)
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0

    skipped = idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )

    assert skipped.reason == "harness-autocompact-off"
    assert store.read("example", "s1").done == {  # type: ignore[union-attr]
        "compact": "skipped:harness-autocompact-off"
    }


def test_failed_compaction_clears_the_window_and_allows_reanchor() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )
    idle.arm(harness="example", session="s1", ttl_seconds=3600)
    assert idle.done(
        "compact", harness="example", session="s1", stretch=1, result="failed"
    ).recorded

    current = store.read("example", "s1")
    assert current is not None
    assert (current.done["compact"], current.compact_window_until_ms) == ("failed", None)
    next_arm = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    assert (next_arm.stretch, next_arm.anchor, next_arm.reason) == (1, 2, None)


def test_return_always_closes_during_compaction_and_late_done_records_same_stretch() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )

    assert idle.return_(harness="example", session="s1", user_prompt=True).stretch_closed == 1
    assert idle.done(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        result="ok",
    ).recorded
    current = store.read("example", "s1")
    assert current is not None and current.stretch_open is False
    assert current.compact_window_until_ms is None


def test_expected_compaction_turn_is_absorbed_after_window() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )
    clock.value += 31_000

    absorbed = idle.arm(harness="example", session="s1", ttl_seconds=3600)

    assert absorbed.reason == "expected-compaction-turn"
    assert store.read("example", "s1").anchor == 1  # type: ignore[union-attr]


def test_late_done_for_previous_stretch_never_touches_new_stretch() -> None:
    idle, clock, store, _ = service()
    armed = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    clock.value = armed.compact_at or 0
    idle.fire(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        anchor=1,
        facts=SAFE_FACTS,
    )
    idle.return_(harness="example", session="s1", user_prompt=True)
    clock.value += 1
    idle.arm(harness="example", session="s1", ttl_seconds=3600)

    assert not idle.done(
        "compact",
        harness="example",
        session="s1",
        stretch=1,
        result="ok",
    ).recorded
    current = store.read("example", "s1")
    assert current is not None and current.stretch == 2 and current.done == {}


def test_closed_stretch_ignores_stale_expected_compaction_turn_on_arm() -> None:
    idle, _, store, _ = service()
    idle.arm(harness="example", session="s1", ttl_seconds=3600)
    current = store.read("example", "s1")
    assert current is not None
    store.states[("example", "s1")] = current.model_copy(
        update={"stretch_open": False, "expect_compaction_turn": True}
    )

    opened = idle.arm(harness="example", session="s1", ttl_seconds=3600)

    assert (opened.stretch, opened.anchor, opened.reason) == (2, 1, None)
    current = store.read("example", "s1")
    assert current is not None
    assert current.stretch_open is True
    assert current.expect_compaction_turn is False
