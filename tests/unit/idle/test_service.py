from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import TypeVar

from meridian.lib.config.models import MeridianConfig
from meridian.lib.harness.idle_types import BackgroundTask, CacheTtl, DetectIdleTtl
from meridian.lib.idle.service import (
    ConfigResult,
    IdleService,
    close_on_launch,
    effective,
)
from meridian.lib.notify import RouteReport, RouteResult
from meridian.lib.state.idle_store import IdleSchedule, IdleState, IdleStore
from meridian.plugin_api.types import HookEventName, Notification

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
        self.notices: list[Notification] = []

    def __call__(
        self, event: HookEventName, notice: Notification
    ) -> RouteReport:
        _ = event
        self.notices.append(notice)
        status = "sent" if self.ok else "failed"
        return RouteReport(results=(RouteResult(route="test", status=status),))


def service(
    *,
    clock: Clock | None = None,
    store: MemoryStore | None = None,
    sender: Sender | None = None,
    config: MeridianConfig | None = None,
    env: dict[str, str] | None = None,
    interactive: bool = False,
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
            interactive=interactive,
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
            interactive=interactive,
        ).config_for("example")

    assert config(env={}, interactive=True).enabled is True
    assert config(env={}, interactive=False).reason == "interactive"
    assert config(env={"MERIDIAN_SESSION_ROLE": "spawn"}, interactive=True).reason == "role"


def test_effective_env_level_beats_more_specific_file_in_both_directions() -> None:
    specific_true = MeridianConfig.model_validate(
        {
            "harness": {"codex": {"idle": {"enabled": True}}},
            "idle": {"enabled": False},
        }
    )
    specific_false = MeridianConfig.model_validate(
        {
            "harness": {"codex": {"idle": {"enabled": False}}},
            "idle": {"enabled": True},
        }
    )

    assert effective("enabled", "codex", specific_true, {"MERIDIAN_IDLE_ENABLED": "0"}) is False
    assert effective("enabled", "codex", specific_false, {"MERIDIAN_IDLE_ENABLED": "1"}) is True
    assert effective("enabled", "codex", specific_true, {}) is True


def test_config_role_and_enabled_gates_do_not_write() -> None:
    disabled, _, disabled_store, _ = service(
        config=MeridianConfig.model_validate({"idle": {"enabled": False}})
    )
    spawn, _, spawn_store, _ = service(env={"MERIDIAN_SESSION_ROLE": "spawn"})

    assert disabled.arm(harness="example", session="s1").reason == "idle-disabled"
    assert spawn.arm(harness="example", session="s1").reason == "role"
    assert disabled.return_(harness="example", session="s1").stretch_closed is None
    assert spawn.return_(harness="example", session="s1").stretch_closed is None
    assert disabled_store.states == {}
    assert spawn_store.states == {}


def test_arm_resolves_explicit_configured_then_detected_ttl() -> None:
    detected: list[str] = []

    def detect_ttl(**kwargs: object) -> CacheTtl:
        detected.append(str(kwargs["session_id"]))
        return CacheTtl(300, estimated=False)

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
    assert store.read("example", "explicit").ttl_estimated is True  # type: ignore[union-attr]
    assert store.read("example", "detected").ttl_estimated is False  # type: ignore[union-attr]
    assert configured_store.read("codex", "configured").ttl_estimated is True  # type: ignore[union-attr]


def test_return_then_long_turn_uses_the_short_push_delay() -> None:
    idle, clock, store, _ = service()
    clock.value = 1_000
    idle.return_(harness="example", session="s1")
    clock.value += 120_000

    armed = idle.arm(harness="example", session="s1")

    assert armed.push_at == clock.value + 60_000
    assert store.read("example", "s1").push_owed_since_ms == 1_000  # type: ignore[union-attr]

    clock.value = armed.push_at
    idle.fire("push", harness="example", session="s1", stretch=1, anchor=1)
    assert store.read("example", "s1").push_owed_since_ms is None  # type: ignore[union-attr]


def test_replacing_a_scheduled_push_keeps_the_original_owed_time() -> None:
    idle, clock, store, _ = service()
    idle.return_(harness="example", session="s1")
    clock.value = 180_000

    delayed = idle.arm(
        harness="example",
        session="s1",
        background_tasks=(BackgroundTask("shell", "npm test"),),
    )
    assert delayed.push_at == 780_000
    assert store.read("example", "s1").push_owed_since_ms == 0  # type: ignore[union-attr]

    clock.value = 181_000
    replaced = idle.arm(harness="example", session="s1")
    assert replaced.push_at == 241_000
    assert store.read("example", "s1").push_owed_since_ms == 0  # type: ignore[union-attr]


def test_close_on_launch_closes_open_row_without_creating_missing_file(tmp_path: Path) -> None:
    env = {"MERIDIAN_HOME": str(tmp_path), "MERIDIAN_SESSION_ROLE": "primary"}
    store = IdleStore(root=tmp_path / "idle")
    store.write(
        IdleState(
            harness="example",
            session="open",
            stretch=1,
            stretch_open=True,
            anchor=1,
            idle_since_ms=1,
            schedule=IdleSchedule(push_at=2),
            updated_at_ms=1,
        )
    )

    close_on_launch(harness="example", session="open", env=env)
    close_on_launch(harness="example", session="missing", env=env)

    current = store.read("example", "open")
    assert current is not None and current.stretch_open is False
    assert IdleService(store=store, config=MeridianConfig(), env=env).status() == ()
    assert not store.path_for("example", "missing").exists()


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
    assert sender.notices[0].body == "Your turn"
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


def test_arm_stores_trimmed_excerpts_and_the_next_arm_replaces_them() -> None:
    idle, _, store, _ = service()

    idle.arm(
        harness="example",
        session="s1",
        ttl_seconds=3600,
        last_user_text="  hello\nworld  ",
        last_assistant_text="reply " * 100,
    )
    first = store.read("example", "s1")
    assert first is not None
    assert first.last_user_text == "hello world"
    assert first.last_assistant_text is not None
    assert len(first.last_assistant_text) <= 280

    idle.arm(harness="example", session="s1", ttl_seconds=3600)
    replaced = store.read("example", "s1")
    assert replaced is not None
    assert (replaced.last_user_text, replaced.last_assistant_text) == (None, None)


def test_arm_does_not_store_excerpts_when_notifications_exclude_messages() -> None:
    idle, _, store, _ = service(
        config=MeridianConfig.model_validate({"notify": {"include_messages": False}})
    )

    idle.arm(
        harness="example",
        session="s1",
        ttl_seconds=3600,
        last_user_text="private prompt",
        last_assistant_text="private answer",
    )

    state = store.read("example", "s1")
    assert state is not None
    assert (state.last_user_text, state.last_assistant_text) == (None, None)


def test_held_push_stays_owed_until_the_turn_the_work_wakes() -> None:
    idle, clock, _store, _sender = service()
    clock.value = 10_000
    idle.return_(harness="example", session="s1")
    clock.value = 20_000
    held = idle.arm(
        harness="example",
        session="s1",
        ttl_seconds=3600,
        background_tasks=(BackgroundTask("shell", "meridian spawn wait"),),
    )
    # The wait yields (3000 s default) before the cache goes cold, so no warning either.
    assert (held.push_at, held.warn_at, held.push_held) == (None, None, "meridian spawns running")

    # The wake turn: the user has waited since their return, so the long-turn delay applies.
    clock.value = 3_000_000
    woken = idle.arm(harness="example", session="s1", ttl_seconds=3600)
    assert (woken.stretch, woken.anchor) == (held.stretch, held.anchor + 1)
    assert (woken.push_at, woken.warn_at) == (3_060_000, 3_000_000 + 45 * 60_000)
