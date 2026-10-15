"""Harness-agnostic idle state machine and notification policy."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.loader import OPTION_CATALOG, load_config
from meridian.lib.config.models import MeridianConfig, NotifyConfig
from meridian.lib.config.schema import ConfigOptionDescriptor, parse_env_scalar
from meridian.lib.harness.idle_types import (
    DetectIdleTtl,
    IdleEvent,
    IdleFacts,
    NativeIdleHook,
    PinnedIdleSession,
)
from meridian.lib.idle.children import (
    SpawnStoreReader,
    active_child_count,
    spawn_store_reader,
)
from meridian.lib.idle.excerpts import trim_turn_excerpts
from meridian.lib.idle.guards import DecisionKind, GuardFacts, decide
from meridian.lib.idle.messages import compaction_notice, stage_notice
from meridian.lib.idle.timeline import Schedule, schedule, select_push_delay
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
from meridian.lib.state.paths import resolve_project_runtime_root_or_none
from meridian.lib.state.session_store import get_session_record
from meridian.lib.state.user_paths import get_user_home

_COMPACT_GRACE_MS = 30_000
logger = logging.getLogger(__name__)


class NotifySender(Protocol):
    """Notification delivery seam for tests."""

    def __call__(self, notice: Notice, cfg: NotifyConfig) -> SendReport: ...


AutocompactOffReader = Callable[[Mapping[str, str]], bool | None]


def _recorded_native_store(
    project_root: Path,
    env: Mapping[str, str],
    harness: str,
) -> Path | None:
    """Read the launch-recorded native store for this managed chat."""

    chat_id = env.get("MERIDIAN_CHAT_ID", "").strip()
    if not chat_id:
        return None
    try:
        runtime_root = resolve_project_runtime_root_or_none(project_root)
        record = get_session_record(runtime_root, chat_id) if runtime_root is not None else None
    except (OSError, ValueError):
        return None
    if record is None or record.harness != harness or not record.native_store:
        return None
    native_store = Path(record.native_store)
    return native_store if native_store.is_absolute() else None


class IdlePolicyConfig(BaseModel):
    """Fully resolved idle policy for one harness operation."""

    model_config = ConfigDict(frozen=True, strict=True)

    enabled: bool
    push_seconds: int
    long_turn_seconds: int
    quick_turn_push_seconds: int
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
        if self.reason is not None:
            payload["reason"] = self.reason
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


def _idle_policy_options() -> dict[str, ConfigOptionDescriptor]:
    options: dict[str, ConfigOptionDescriptor] = {}
    for option in OPTION_CATALOG.options:
        path = option.field_path
        if path[:1] == ("idle",):
            options[path[-1]] = option
        elif len(path) == 4 and path[0] == "harness" and path[2] == "idle":
            options.setdefault(path[-1], option)
    return options


_IDLE_POLICY_OPTIONS = _idle_policy_options()


def _env_value(
    env: Mapping[str, str],
    option: ConfigOptionDescriptor | None,
) -> object | None:
    if option is None or option.primary_env_var is None:
        return None
    raw = env.get(option.primary_env_var)
    if raw is None:
        return None
    return parse_env_scalar(
        value_kind=option.value_kind,
        raw_value=raw,
        env_name=option.primary_env_var,
    )


def effective(
    field: str,
    harness: str,
    config: MeridianConfig,
    env: Mapping[str, str] | None = None,
) -> object:
    """Resolve level-first, then most-specific idle precedence."""

    policy_option = _IDLE_POLICY_OPTIONS.get(field)
    if policy_option is None:
        raise ValueError(f"Unknown idle config field: {field!r}")
    values = os.environ if env is None else env
    normalized_harness = harness.strip().lower()
    profile = config.harness_profile(normalized_harness)
    specific_option = OPTION_CATALOG.option_for_field_path(
        ("harness", normalized_harness, "idle", field)
    )
    global_option = OPTION_CATALOG.option_for_field_path(("idle", field))

    value = _env_value(values, specific_option)
    if value is not None:
        return value
    value = _env_value(values, global_option)
    if value is not None:
        return value

    if (
        specific_option is not None
        and profile is not None
        and field in profile.idle.model_fields_set
    ):
        return getattr(profile.idle, field)
    if global_option is not None:
        return getattr(config.idle, field)
    if specific_option is not None and profile is not None:
        return getattr(profile.idle, field)
    return None


def resolve_policy(
    harness: str,
    config: MeridianConfig,
    env: Mapping[str, str] | None = None,
) -> IdlePolicyConfig:
    """Resolve all policy fields once for an operation."""

    values = {field: effective(field, harness, config, env) for field in _IDLE_POLICY_OPTIONS}
    return IdlePolicyConfig.model_validate(values)


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


def _close_open_stretch(state: IdleState, *, returned_at_ms: int) -> IdleState:
    return state.model_copy(
        update={
            "stretch_open": False,
            "returned_at_ms": returned_at_ms,
            "compact_window_until_ms": None,
            "expect_compaction_turn": False,
        }
    )


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
        native_idle_hook: NativeIdleHook | None = None,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        configured_role = self.env.get("MERIDIAN_SESSION_ROLE")
        self.role = "primary" if configured_role is None and interactive else configured_role
        self.project_root = project_root or Path(self.env.get("MERIDIAN_PROJECT_DIR", Path.cwd()))
        self.config = config if config is not None else load_config(self.project_root)
        self._now_ms = now_ms
        self.store = store if store is not None else IdleStore(now_ms=now_ms)
        self._notify_sender = notify_sender if notify_sender is not None else send
        self._spawn_reader = spawn_reader or spawn_store_reader(self.env)
        self._autocompact_off = autocompact_off
        self._detect_ttl = detect_ttl
        self._native_idle_hook = native_idle_hook

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
        last_user_text: str | None = None,
        last_assistant_text: str | None = None,
        provider: str | None = None,
    ) -> ArmResult:
        config_result = self.config_for(harness)
        if not config_result.enabled:
            return ArmResult(None, None, reason=config_result.reason)
        policy = config_result.policy
        resolved_ttl = ttl_seconds if ttl_seconds is not None else policy.ttl_seconds
        ttl_estimated = True  # Configured/default TTLs have no provider guarantee.
        if resolved_ttl is None and self._detect_ttl is not None:
            detected = self._detect_ttl(
                session_id=session,
                native_store=_recorded_native_store(self.project_root, self.env, harness),
                provider=provider,
                env=self.env,
            )
            if detected is not None:
                resolved_ttl = detected.seconds
                ttl_estimated = detected.estimated
        if resolved_ttl is not None and resolved_ttl <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if input_count is not None and input_count < 0:
            raise ValueError("input_count must be zero or greater")
        if self._notify_config().include_messages:
            user_excerpt, assistant_excerpt = trim_turn_excerpts(
                last_user_text,
                last_assistant_text,
            )
        else:
            user_excerpt, assistant_excerpt = None, None
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
            if current is None or current.idle_since_ms is None:
                stretch = 1
                anchor = 1
            else:
                stretch = current.stretch + int(opens_new)
                anchor = 1 if opens_new else current.anchor + 1
            done = {} if opens_new or current is None else dict(current.done)
            returned_at_ms = (
                now_ms
                if implies_return
                else (current.returned_at_ms if current is not None else None)
            )
            previous_anchor_ms = current.idle_since_ms if current is not None else None
            placed = schedule(
                now_ms,
                resolved_ttl,
                policy,
                push_delay_seconds=select_push_delay(
                    now_ms,
                    returned_at_ms,
                    previous_anchor_ms,
                    policy,
                ),
            )
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
                last_user_text=user_excerpt,
                last_assistant_text=assistant_excerpt,
                anchor=anchor,
                idle_since_ms=now_ms,
                returned_at_ms=returned_at_ms,
                ttl_seconds=resolved_ttl,
                ttl_estimated=ttl_estimated,
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
    ) -> ReturnResult:
        if not self.config_for(harness).enabled:
            return ReturnResult(stretch_closed=None, was_open=False)

        now_ms = self._now_ms()

        def transition(current: IdleState | None) -> tuple[IdleState | None, ReturnResult]:
            if current is None:
                next_state = IdleState(
                    harness=harness,
                    session=session,
                    stretch=1,
                    stretch_open=False,
                    anchor=1,
                    idle_since_ms=None,
                    returned_at_ms=now_ms,
                    ttl_seconds=None,
                    schedule=IdleSchedule(),
                    updated_at_ms=now_ms,
                )
                return next_state, ReturnResult(stretch_closed=None, was_open=False)
            if not current.stretch_open:
                next_state = current.model_copy(update={"returned_at_ms": now_ms})
                stretch_closed = current.stretch if current.idle_since_ms is not None else None
                return next_state, ReturnResult(stretch_closed=stretch_closed, was_open=False)
            next_state = _close_open_stretch(current, returned_at_ms=now_ms)
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
            raise ValueError("stretch must be greater than zero")
        if anchor < 1:
            raise ValueError("anchor must be greater than zero")
        policy = resolve_policy(harness, self.config, self.env)
        observed = facts or IdleFacts(
            busy=False,
            agents_running=0,
            context_tokens=None,
            harness_autocompact_off=False,
        )
        if observed.agents_running < 0:
            raise ValueError("agents_running must not be negative")
        if observed.context_tokens is not None and observed.context_tokens < 0:
            raise ValueError("context_tokens must not be negative")
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
            busy=observed.busy,
            agents_running=observed.agents_running,
            child_spawns_active=self._children_active() if stage == "compact" else False,
            context_tokens=observed.context_tokens,
            harness_autocompact_off=(observed.harness_autocompact_off or env_autocompact_off),
        )
        now_ms = self._now_ms()

        def transition(
            current: IdleState | None,
        ) -> tuple[IdleState | None, tuple[FireResult, IdleState | None]]:
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
                    return current, (result, None)
                done = dict(current.done)
                done[stage] = f"skipped:{decision.reason}"
                next_state = current.model_copy(update={"done": done})
                return next_state, (result, None)

            if current is None:  # pragma: no cover - guarded by decide()
                raise RuntimeError("idle stage cannot be claimed without state")
            done = dict(current.done)
            done[stage] = "claimed" if stage == "compact" else "sent"
            updates: dict[str, object] = {"done": done}
            if stage == "compact":
                updates.update(
                    compact_window_until_ms=now_ms + _COMPACT_GRACE_MS,
                    expect_compaction_turn=True,
                )
            claimed_state = current.model_copy(update=updates)
            return claimed_state, (result, claimed_state)

        result, claimed_state = self.store.mutate(harness, session, transition)
        if (
            result.decision == "act"
            and claimed_state is not None
            and (stage == "push" or stage == "warn")
        ):
            self._send_stage_notice(
                stage,
                harness=harness,
                session=session,
                stretch=stretch,
                state=claimed_state,
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
        state: IdleState,
        policy: IdlePolicyConfig,
    ) -> None:
        ok = False
        try:
            label = build_session_label(environ=self.env)
            notice = stage_notice(
                stage,
                state,
                label,
                include_messages=self._notify_config().include_messages,
                now_ms=self._now_ms(),
                warn_email=policy.warn_email,
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
            raise ValueError("stretch must be greater than zero")
        if result not in {"ok", "failed", "vetoed"}:
            raise ValueError("result must be one of: ok, failed, vetoed")
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
            with suppress(Exception):
                self._notify_sender(
                    compaction_notice(
                        result,
                        detail,
                        build_session_label(environ=self.env),
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
                last_user_text=event.last_user_text,
                last_assistant_text=event.last_assistant_text,
                provider=event.provider,
            )
        return self.return_(
            harness=harness,
            session=event.harness_session_id,
        )

    def apply_native_event(
        self,
        payload: str,
        *,
        harness: str,
    ) -> None:
        """Observe one intercepted callback, then always delegate it downstream."""

        hook = self._native_idle_hook
        if hook is None:
            raise ValueError(f"harness {harness} does not accept idle events")

        def read_session(session: str) -> PinnedIdleSession | None:
            states = self.status(harness=harness, session=session)
            if not states:
                return None
            return PinnedIdleSession(last_input_count=states[0].last_input_count)

        try:
            if not payload.strip():
                raise ValueError("idle event requires a payload argument or stdin")
            event = hook.observe(payload, read_session)
            if event is not None:
                self.event(event, harness=harness)
        finally:
            hook.delegate(payload, self.env)

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


def close_on_launch(*, harness: str, session: str, env: Mapping[str, str]) -> None:
    """Close an open idle stretch before Meridian reopens its harness session."""

    if not session:
        return
    try:
        store = IdleStore(root=get_user_home(env=env) / "idle")
        current = store.read(harness, session)
        if current is None or not current.stretch_open:
            return
        returned_at_ms = _default_now_ms()

        def transition(state: IdleState | None) -> tuple[IdleState | None, None]:
            if state is None or not state.stretch_open:
                return None, None
            return _close_open_stretch(state, returned_at_ms=returned_at_ms), None

        store.mutate(harness, session, transition)
    except Exception:
        logger.debug(
            "Failed to close idle stretch before reopening %s session %s",
            harness,
            session,
            exc_info=True,
        )


__all__ = [
    "ArmResult",
    "AutocompactOffReader",
    "ConfigResult",
    "DoneResult",
    "FireResult",
    "IdlePolicyConfig",
    "IdleService",
    "NotifySender",
    "ReturnResult",
    "close_on_launch",
    "effective",
    "resolve_policy",
]
