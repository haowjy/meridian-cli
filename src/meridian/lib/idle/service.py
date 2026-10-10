"""Harness-agnostic idle state machine and notification policy."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel

from meridian.lib.config.schema import parse_env_scalar
from meridian.lib.config.settings import MeridianConfig, NotifyConfig, load_config
from meridian.lib.harness.idle_types import (
    DetectIdleTtl,
    IdleEvent,
    IdleFacts,
    PinnedIdleSession,
)
from meridian.lib.idle.children import (
    SpawnStoreReader,
    active_child_count,
    spawn_store_reader,
)
from meridian.lib.idle.guards import DecisionKind, GuardFacts, decide
from meridian.lib.idle.timeline import Schedule, schedule
from meridian.lib.notify import Notice, SendReport, send
from meridian.lib.notify.label import build_session_label
from meridian.lib.state.idle_store import (
    CompactResultValue,
    IdleSchedule,
    IdleState,
    IdleStore,
    IdleStoreReader,
    Stage,
)

_COMPACT_GRACE_MS = 30_000


class NotifySender(Protocol):
    """Notification delivery seam for tests."""

    def __call__(self, notice: Notice, cfg: NotifyConfig) -> SendReport: ...


AutocompactOffReader = Callable[[Mapping[str, str]], bool | None]


class NativeEventParser(Protocol):
    """Translate a native callback without giving the harness direct store access."""

    def __call__(
        self,
        payload: str,
        *,
        session_reader: Callable[[str], PinnedIdleSession | None],
    ) -> IdleEvent | None: ...


@dataclass(frozen=True)
class IdlePolicyConfig:
    enabled: bool
    push_seconds: int
    warn_minutes: int
    warn_email: bool
    compact_minutes: int
    compact: bool
    min_compact_tokens: int
    late_fire_tolerance_seconds: int
    ttl_seconds: int | None


@dataclass(frozen=True)
class ConfigResult:
    enabled: bool
    reason: str | None
    policy: IdlePolicyConfig

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "push_seconds": self.policy.push_seconds,
            "warn_minutes": self.policy.warn_minutes,
            "compact_minutes": self.policy.compact_minutes,
            "compact": self.policy.compact,
            "min_compact_tokens": self.policy.min_compact_tokens,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.policy.ttl_seconds is not None:
            payload["ttl_seconds"] = self.policy.ttl_seconds
        return payload


@dataclass(frozen=True)
class ArmResult:
    stretch: int | None
    anchor: int | None
    push_at: int | None = None
    warn_at: int | None = None
    compact_at: int | None = None
    reason: str | None = None

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {"stretch": self.stretch, "anchor": self.anchor}
        for field in ("push_at", "warn_at", "compact_at"):
            value = getattr(self, field)
            if value is not None:
                payload[field] = value
        return payload


@dataclass(frozen=True)
class ReturnResult:
    stretch_closed: int | None
    was_open: bool

    def to_wire(self) -> dict[str, object]:
        return {"stretch_closed": self.stretch_closed is not None, "was_open": self.was_open}


@dataclass(frozen=True)
class FireResult:
    decision: DecisionKind
    reason: str

    def to_wire(self) -> dict[str, object]:
        return {"decision": self.decision, "reason": self.reason}


@dataclass(frozen=True)
class DoneResult:
    recorded: bool

    def to_wire(self) -> dict[str, object]:
        return {}


def _default_now_ms() -> int:
    return int(time.time() * 1000)


_FIELD_KINDS: dict[str, Literal["bool", "int"]] = {
    "enabled": "bool",
    "push_seconds": "int",
    "warn_minutes": "int",
    "warn_email": "bool",
    "compact_minutes": "int",
    "compact": "bool",
    "min_compact_tokens": "int",
    "late_fire_tolerance_seconds": "int",
    "ttl_seconds": "int",
}
_HARNESS_FIELDS = frozenset({"enabled", "compact", "ttl_seconds"})


def _env_value(env: Mapping[str, str], name: str, field: str) -> object | None:
    raw = env.get(name)
    if raw is None:
        return None
    return parse_env_scalar(value_kind=_FIELD_KINDS[field], raw_value=raw, env_name=name)


def _harness_idle_config(config: MeridianConfig, harness: str) -> BaseModel | None:
    profile = getattr(config.harness, harness.strip().lower(), None)
    idle = getattr(profile, "idle", None)
    return idle if isinstance(idle, BaseModel) else None


def effective(
    field: str,
    harness: str,
    config: MeridianConfig,
    env: Mapping[str, str] | None = None,
) -> object:
    """Resolve level-first, then most-specific idle precedence."""

    if field not in _FIELD_KINDS:
        raise ValueError(f"Unknown idle config field: {field!r}")
    values = os.environ if env is None else env
    normalized_harness = harness.strip().lower()
    harness_idle = _harness_idle_config(config, normalized_harness)

    if field in _HARNESS_FIELDS:
        specific_env = f"MERIDIAN_HARNESS_IDLE_{field.upper()}_{normalized_harness.upper()}"
        value = _env_value(values, specific_env, field)
        if value is not None:
            return value

    if hasattr(config.idle, field):
        global_env = f"MERIDIAN_IDLE_{field.upper()}"
        value = _env_value(values, global_env, field)
        if value is not None:
            return value

    if (
        field in _HARNESS_FIELDS
        and harness_idle is not None
        and field in harness_idle.model_fields_set
    ):
        return getattr(harness_idle, field)
    if hasattr(config.idle, field):
        return getattr(config.idle, field)
    if harness_idle is not None and hasattr(harness_idle, field):
        return getattr(harness_idle, field)
    return None


def resolve_policy(
    harness: str,
    config: MeridianConfig,
    env: Mapping[str, str] | None = None,
) -> IdlePolicyConfig:
    """Resolve all policy fields once for an operation."""

    values = {field: effective(field, harness, config, env) for field in _FIELD_KINDS}
    return IdlePolicyConfig(
        enabled=bool(values["enabled"]),
        push_seconds=int(cast("int", values["push_seconds"])),
        warn_minutes=int(cast("int", values["warn_minutes"])),
        warn_email=bool(values["warn_email"]),
        compact_minutes=int(cast("int", values["compact_minutes"])),
        compact=bool(values["compact"]),
        min_compact_tokens=int(cast("int", values["min_compact_tokens"])),
        late_fire_tolerance_seconds=int(cast("int", values["late_fire_tolerance_seconds"])),
        ttl_seconds=(
            int(cast("int", values["ttl_seconds"])) if values["ttl_seconds"] is not None else None
        ),
    )


def _result_for_state(state: IdleState, *, reason: str | None = None) -> ArmResult:
    done = state.done
    return ArmResult(
        stretch=state.stretch,
        anchor=state.anchor,
        push_at=None if reason is not None or "push" in done else state.schedule.push_at,
        warn_at=None if reason is not None or "warn" in done else state.schedule.warn_at,
        compact_at=None if reason is not None or "compact" in done else state.schedule.compact_at,
        reason=reason,
    )


def _persisted_schedule(value: Schedule) -> IdleSchedule:
    return IdleSchedule(
        push_at=value.push_at,
        warn_at=value.warn_at,
        compact_at=value.compact_at,
    )


def wire_payload(result: object) -> object:
    """Serialize a service result for the adapter-facing CLI contract."""

    if result is None:
        return {}
    if isinstance(result, (ConfigResult, ArmResult, ReturnResult, FireResult, DoneResult)):
        return result.to_wire()
    if isinstance(result, tuple):
        states = cast("tuple[IdleState, ...]", result)
        return [state.model_dump(mode="json") for state in states]
    raise TypeError(f"unsupported idle result: {type(result).__name__}")


class IdleService:
    """Synchronous policy API shared by the CLI and launcher sidecar."""

    def __init__(
        self,
        *,
        store: IdleStoreReader | None = None,
        config: MeridianConfig | None = None,
        env: Mapping[str, str] | None = None,
        now_ms: Callable[[], int] = _default_now_ms,
        notify_sender: NotifySender | None = None,
        spawn_reader: SpawnStoreReader | None = None,
        project_root: Path | None = None,
        interactive: bool = False,
        autocompact_off: AutocompactOffReader | None = None,
        detect_ttl: DetectIdleTtl | None = None,
        native_event_parser: NativeEventParser | None = None,
        native_event_applied: Callable[[str, Mapping[str, str]], None] | None = None,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        configured_role = self.env.get("MERIDIAN_SESSION_ROLE")
        self.role = "primary" if configured_role is None and interactive else configured_role
        root = project_root or Path(self.env.get("MERIDIAN_PROJECT_DIR", Path.cwd()))
        self.config = config if config is not None else load_config(root, resolve_models=False)
        self._now_ms = now_ms
        self.store = store if store is not None else IdleStore(now_ms=now_ms)
        self._notify_sender = notify_sender if notify_sender is not None else send
        self._spawn_reader = spawn_reader or spawn_store_reader(self.env)
        self._autocompact_off = autocompact_off
        self._detect_ttl = detect_ttl
        self._native_event_parser = native_event_parser
        self._native_event_applied = native_event_applied

    def config_for(self, harness: str) -> ConfigResult:
        policy = resolve_policy(harness, self.config, self.env)
        if self.role != "primary":
            reason = "interactive" if self.role is None else "role"
            return ConfigResult(enabled=False, reason=reason, policy=policy)
        if not policy.enabled:
            return ConfigResult(enabled=False, reason="idle-disabled", policy=policy)
        return ConfigResult(enabled=True, reason=None, policy=policy)

    def arm(
        self,
        *,
        harness: str,
        session: str,
        ttl_seconds: int | None = None,
        implies_return: bool = False,
        turn_id: str | None = None,
        input_count: int | None = None,
        cwd: Path | None = None,
        provider: str | None = None,
    ) -> ArmResult:
        config_result = self.config_for(harness)
        if not config_result.enabled:
            return ArmResult(None, None, reason=config_result.reason)
        policy = config_result.policy
        resolved_ttl = ttl_seconds if ttl_seconds is not None else policy.ttl_seconds
        if resolved_ttl is None and self._detect_ttl is not None:
            resolved_ttl = self._detect_ttl(
                session_id=session,
                cwd=cwd,
                provider=provider,
                env=self.env,
            )
        if resolved_ttl is not None and resolved_ttl <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if input_count is not None and input_count < 0:
            raise ValueError("input_count must be zero or greater")
        now_ms = self._now_ms()

        def transition(current: IdleState | None) -> tuple[IdleState, ArmResult]:
            if (
                implies_return
                and turn_id is not None
                and current is not None
                and current.last_turn_id == turn_id
            ):
                next_state = current.model_copy(
                    update={
                        "last_input_count": (
                            input_count if input_count is not None else current.last_input_count
                        )
                    }
                )
                return next_state, _result_for_state(
                    next_state,
                    reason="duplicate-turn",
                )

            if current is not None and not implies_return:
                window_active = (
                    current.compact_window_until_ms is not None
                    and now_ms <= current.compact_window_until_ms
                )
                if window_active:
                    next_state = current.model_copy(
                        update={
                            "expect_compaction_turn": False,
                            "last_input_count": (
                                input_count if input_count is not None else current.last_input_count
                            ),
                        }
                    )
                    return next_state, _result_for_state(next_state, reason="compact-window")
                if current.stretch_open and current.expect_compaction_turn:
                    next_state = current.model_copy(
                        update={
                            "expect_compaction_turn": False,
                            "last_input_count": (
                                input_count if input_count is not None else current.last_input_count
                            ),
                        }
                    )
                    return next_state, _result_for_state(
                        next_state,
                        reason="expected-compaction-turn",
                    )
                if current.stretch_open and current.done.get("compact") == "ok":
                    next_state = current.model_copy(
                        update={
                            "last_input_count": (
                                input_count if input_count is not None else current.last_input_count
                            )
                        }
                    )
                    return next_state, _result_for_state(
                        next_state,
                        reason="already-compacted",
                    )

            opens_new = current is None or not current.stretch_open or implies_return
            if current is None:
                stretch = 1
                anchor = 1
            else:
                stretch = current.stretch + int(opens_new)
                anchor = 1 if opens_new else current.anchor + 1
            done = {} if opens_new or current is None else dict(current.done)
            placed = schedule(now_ms, resolved_ttl, policy)
            next_state = IdleState(
                harness=harness,
                session=session,
                stretch=stretch,
                stretch_open=True,
                last_turn_id=(
                    turn_id
                    if implies_return
                    else (current.last_turn_id if current is not None and not opens_new else None)
                ),
                last_input_count=(
                    input_count
                    if input_count is not None
                    else (current.last_input_count if current is not None else None)
                ),
                anchor=anchor,
                idle_since_ms=now_ms,
                ttl_seconds=resolved_ttl,
                schedule=_persisted_schedule(placed),
                done=done,
                compact_window_until_ms=None,
                expect_compaction_turn=False,
                updated_at_ms=now_ms,
            )
            return next_state, _result_for_state(next_state)

        return self.store.mutate(harness, session, transition)

    def return_(
        self,
        *,
        harness: str,
        session: str,
        user_prompt: bool,
    ) -> ReturnResult:
        if not user_prompt:
            raise ValueError("--user-prompt is required")

        def transition(current: IdleState | None) -> tuple[IdleState | None, ReturnResult]:
            if current is None:
                return None, ReturnResult(stretch_closed=None, was_open=False)
            if not current.stretch_open:
                return current, ReturnResult(stretch_closed=current.stretch, was_open=False)
            next_state = current.model_copy(
                update={
                    "stretch_open": False,
                    "compact_window_until_ms": None,
                    "expect_compaction_turn": False,
                }
            )
            return next_state, ReturnResult(stretch_closed=current.stretch, was_open=True)

        return self.store.mutate(harness, session, transition)

    def _children_active(self) -> bool:
        spawn_id = self.env.get("MERIDIAN_SPAWN_ID")
        if not spawn_id:
            return False
        try:
            return active_child_count(spawn_id, self._spawn_reader()) > 0
        except Exception:
            return True

    def fire(
        self,
        stage: Stage,
        *,
        harness: str,
        session: str,
        stretch: int,
        anchor: int,
        facts: IdleFacts | None = None,
    ) -> FireResult:
        if stage not in {"push", "warn", "compact"}:
            raise ValueError(f"unknown idle stage: {stage}")
        if stretch < 1:
            raise ValueError("--stretch must be greater than zero")
        if anchor < 1:
            raise ValueError("--anchor must be greater than zero")
        policy = resolve_policy(harness, self.config, self.env)
        observed = facts or IdleFacts(
            draft="unknown",
            busy=False,
            agents_running=0,
            context_tokens=None,
            harness_autocompact_off=False,
        )
        if observed.draft not in {"yes", "no", "unknown"}:
            raise ValueError("--draft must be one of: yes, no, unknown")
        if observed.agents_running < 0:
            raise ValueError("--agents-running must not be negative")
        if observed.context_tokens is not None and observed.context_tokens < 0:
            raise ValueError("--context-tokens must not be negative")
        env_autocompact_off = (
            stage == "compact"
            and self._autocompact_off is not None
            and bool(self._autocompact_off(self.env))
        )
        guard_facts = GuardFacts(
            stretch=stretch,
            anchor=anchor,
            role=self.role,
            harness_enabled=policy.enabled,
            draft=observed.draft,
            busy=observed.busy,
            agents_running=observed.agents_running,
            child_spawns_active=self._children_active() if stage == "compact" else False,
            context_tokens=observed.context_tokens,
            harness_autocompact_off=(observed.harness_autocompact_off or env_autocompact_off),
        )
        now_ms = self._now_ms()

        def transition(current: IdleState | None) -> tuple[IdleState | None, FireResult]:
            decision = decide(stage, guard_facts, current, policy, now_ms)
            result = FireResult(decision.decision, decision.reason)
            if decision.decision == "skip":
                transient = {
                    "not-primary",
                    "idle-disabled",
                    "stage-done",
                    "stretch-closed",
                    "stale-anchor",
                    "not-scheduled",
                }
                if current is None or decision.reason in transient:
                    return current, result
                done = dict(current.done)
                done[stage] = f"skipped:{decision.reason}"
                return current.model_copy(update={"done": done}), result

            assert current is not None
            done = dict(current.done)
            done[stage] = "claimed" if stage == "compact" else "sent"
            updates: dict[str, object] = {"done": done}
            if stage == "compact":
                updates.update(
                    compact_window_until_ms=now_ms + _COMPACT_GRACE_MS,
                    expect_compaction_turn=True,
                )
            return current.model_copy(update=updates), result

        result = self.store.mutate(harness, session, transition)
        if result.decision == "act" and (stage == "push" or stage == "warn"):
            self._send_stage_notice(
                stage,
                harness=harness,
                session=session,
                stretch=stretch,
                policy=policy,
            )
        return result

    def _send_stage_notice(
        self,
        stage: Literal["push", "warn"],
        *,
        harness: str,
        session: str,
        stretch: int,
        policy: IdlePolicyConfig,
    ) -> None:
        ok = False
        try:
            title = build_session_label().titled()
            if stage == "push":
                notice = Notice(
                    title=title,
                    body="waiting on you",
                    priority=3,
                    email=False,
                    kind="idle",
                )
            else:
                notice = Notice(
                    title=title,
                    body=f"cache cold in {policy.warn_minutes}m",
                    priority=4,
                    email=policy.warn_email,
                    kind="idle",
                )
            ok = self._notify_sender(notice, self._notify_config()).ok
        except Exception:
            ok = False
        if not ok:
            self._mark_notification_failed(harness, session, stretch, stage)

    def _notify_config(self) -> NotifyConfig:
        return self.config.notify

    def _mark_notification_failed(
        self,
        harness: str,
        session: str,
        stretch: int,
        stage: Literal["push", "warn"],
    ) -> None:
        def transition(current: IdleState | None) -> tuple[IdleState | None, None]:
            if current is None or current.stretch != stretch or current.done.get(stage) != "sent":
                return current, None
            done = dict(current.done)
            done[stage] = "failed"
            return current.model_copy(update={"done": done}), None

        self.store.mutate(harness, session, transition)

    def done(
        self,
        stage: Literal["compact"],
        *,
        harness: str,
        session: str,
        stretch: int,
        result: CompactResultValue,
        detail: str | None = None,
    ) -> DoneResult:
        if stage != "compact":
            raise ValueError(f"unknown idle completion stage: {stage}")
        if stretch < 1:
            raise ValueError("--stretch must be greater than zero")
        if result not in {"ok", "failed", "vetoed"}:
            raise ValueError("--result must be one of: ok, failed, vetoed")
        now_ms = self._now_ms()

        def transition(current: IdleState | None) -> tuple[IdleState | None, DoneResult]:
            if (
                current is None
                or current.stretch != stretch
                or current.done.get(stage) != "claimed"
            ):
                return current, DoneResult(recorded=False)
            done = dict(current.done)
            done[stage] = result
            updates: dict[str, object] = {"done": done}
            if result == "ok" and current.stretch_open:
                updates["compact_window_until_ms"] = now_ms + _COMPACT_GRACE_MS
            elif result != "ok":
                updates.update(
                    compact_window_until_ms=None,
                    expect_compaction_turn=False,
                )
            return current.model_copy(update=updates), DoneResult(recorded=True)

        done_result = self.store.mutate(harness, session, transition)
        if done_result.recorded:
            body = {
                "ok": "compacted",
                "failed": "compaction failed",
                "vetoed": "compaction vetoed",
            }[result]
            if detail:
                body = f"{body} ({detail})"
            with suppress(Exception):
                self._notify_sender(
                    Notice(
                        title=build_session_label().titled(),
                        body=body,
                        priority=3 if result == "ok" else 4,
                        email=False,
                        kind="idle",
                    ),
                    self._notify_config(),
                )
        return done_result

    def event(
        self,
        event: IdleEvent,
        *,
        harness: str,
        ttl_seconds: int | None = None,
    ) -> ArmResult | ReturnResult:
        if event.kind == "turn_end":
            resolved_ttl = event.ttl_seconds if ttl_seconds is None else ttl_seconds
            return self.arm(
                harness=harness,
                session=event.harness_session_id,
                ttl_seconds=resolved_ttl,
                implies_return=event.implies_return,
                turn_id=event.turn_id,
                input_count=event.input_count,
            )
        return self.return_(
            harness=harness,
            session=event.harness_session_id,
            user_prompt=True,
        )

    def apply_native_event(
        self,
        payload: str,
        *,
        harness: str,
    ) -> None:
        """Parse and apply one harness callback through the policy service."""

        if not payload.strip():
            raise ValueError("idle event requires a payload argument or stdin")
        if self._native_event_parser is None:
            raise ValueError(f"harness {harness} does not accept idle events")

        def read_session(session: str) -> PinnedIdleSession | None:
            states = self.status(harness=harness, session=session)
            if not states:
                return None
            return PinnedIdleSession(last_input_count=states[0].last_input_count)

        event = self._native_event_parser(payload, session_reader=read_session)
        if event is None:
            return
        self.event(event, harness=harness)
        if self._native_event_applied is not None:
            self._native_event_applied(payload, self.env)

    def status(
        self,
        *,
        harness: str | None = None,
        session: str | None = None,
    ) -> tuple[IdleState, ...]:
        states = self.store.list_states()
        return tuple(
            state
            for state in states
            if state.stretch_open
            and (harness is None or state.harness == harness)
            and (session is None or state.session == session)
        )


__all__ = [
    "ArmResult",
    "AutocompactOffReader",
    "ConfigResult",
    "DoneResult",
    "FireResult",
    "IdlePolicyConfig",
    "IdleService",
    "NativeEventParser",
    "NotifySender",
    "ReturnResult",
    "effective",
    "resolve_policy",
    "wire_payload",
]
