"""Harness-agnostic idle state machine and notification policy."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.loader import OPTION_CATALOG, load_config
from meridian.lib.config.models import MeridianConfig, NotifyConfig
from meridian.lib.config.schema import ConfigOptionDescriptor, parse_env_scalar
from meridian.lib.harness.idle_types import (
    BackgroundTask,
    DetectIdleTtl,
    IdleEvent,
    NativeIdleHook,
    PinnedIdleSession,
)
from meridian.lib.idle.excerpts import trim_turn_excerpts
from meridian.lib.idle.guards import DecisionKind, GuardFacts, decide
from meridian.lib.idle.messages import STAGE_EVENTS, stage_notice
from meridian.lib.idle.timeline import Schedule, push_delay, schedule
from meridian.lib.idle.wake import expected_wake
from meridian.lib.notify import RouteReport
from meridian.lib.notify.emit import emit
from meridian.lib.notify.label import build_session_label
from meridian.lib.state.idle_store import (
    IdleSchedule,
    IdleState,
    IdleStore,
    IdleStoreReader,
    Stage,
)
from meridian.lib.state.paths import resolve_project_runtime_root_or_none
from meridian.lib.state.session_store import chat_record
from meridian.lib.state.user_paths import get_user_home
from meridian.plugin_api.types import HookEventName, Notification

logger = logging.getLogger(__name__)


class NotifySender(Protocol):
    """Emits a notification as its hook event; a seam for tests."""

    def __call__(
        self, event: HookEventName, notification: Notification
    ) -> RouteReport: ...


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
        record = chat_record(runtime_root, chat_id) if runtime_root is not None else None
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
    reason: str | None = None
    push_held: str | None = None

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {"stretch": self.stretch, "anchor": self.anchor}
        for field in ("push_at", "warn_at", "push_held"):
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


def _result_for_state(
    state: IdleState,
    *,
    reason: str | None = None,
    push_held: str | None = None,
) -> ArmResult:
    done = state.done
    return ArmResult(
        stretch=state.stretch,
        anchor=state.anchor,
        push_at=None if reason is not None or "push" in done else state.schedule.push_at,
        warn_at=None if reason is not None or "warn" in done else state.schedule.warn_at,
        reason=reason,
        push_held=push_held,
    )


def _persisted_schedule(value: Schedule) -> IdleSchedule:
    return IdleSchedule(
        push_at=value.push_at,
        warn_at=value.warn_at,
    )


def _close_open_stretch(state: IdleState, *, returned_at_ms: int) -> IdleState:
    return state.model_copy(
        update={"stretch_open": False, "push_owed_since_ms": returned_at_ms}
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
        project_root: Path | None = None,
        interactive: bool = False,
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
        self._notify_sender = notify_sender or self._emit
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
        background_tasks: Sequence[BackgroundTask] = (),
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
        wake = expected_wake(
            background_tasks,
            now_ms=now_ms,
            default_yield_seconds=self.config.wait_yield_seconds_for_harness(harness),
        )

        def transition(current: IdleState | None) -> tuple[IdleState, ArmResult]:
            kept_input = input_count if input_count is not None else (
                current.last_input_count if current is not None else None
            )
            if (
                implies_return
                and turn_id is not None
                and current is not None
                and current.last_turn_id == turn_id
            ):
                next_state = current.model_copy(update={"last_input_count": kept_input})
                return next_state, _result_for_state(next_state, reason="duplicate-turn")

            opens_new = current is None or not current.stretch_open or implies_return
            if current is None or current.idle_since_ms is None:
                stretch = 1
                anchor = 1
            else:
                stretch = current.stretch + int(opens_new)
                anchor = 1 if opens_new else current.anchor + 1
            done = {} if opens_new or current is None else dict(current.done)
            owed_since_ms = (
                now_ms
                if implies_return
                else (current.push_owed_since_ms if current is not None else None)
            )
            placed = schedule(
                now_ms,
                resolved_ttl,
                policy,
                push_delay_seconds=push_delay(now_ms, owed_since_ms, policy),
                wake=wake,
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
                last_input_count=kept_input,
                last_user_text=user_excerpt,
                last_assistant_text=assistant_excerpt,
                anchor=anchor,
                idle_since_ms=now_ms,
                # Scheduling does not answer the response; delivery or return does.
                push_owed_since_ms=owed_since_ms,
                ttl_seconds=resolved_ttl,
                ttl_estimated=ttl_estimated,
                schedule=_persisted_schedule(placed),
                done=done,
                updated_at_ms=now_ms,
            )
            return next_state, _result_for_state(next_state, push_held=wake.certain)

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
                    push_owed_since_ms=now_ms,
                    ttl_seconds=None,
                    schedule=IdleSchedule(),
                    updated_at_ms=now_ms,
                )
                return next_state, ReturnResult(stretch_closed=None, was_open=False)
            if not current.stretch_open:
                next_state = current.model_copy(update={"push_owed_since_ms": now_ms})
                stretch_closed = current.stretch if current.idle_since_ms is not None else None
                return next_state, ReturnResult(stretch_closed=stretch_closed, was_open=False)
            next_state = _close_open_stretch(current, returned_at_ms=now_ms)
            return next_state, ReturnResult(stretch_closed=current.stretch, was_open=True)

        return self.store.mutate(harness, session, transition)

    def fire(
        self,
        stage: Stage,
        *,
        harness: str,
        session: str,
        stretch: int,
        anchor: int,
    ) -> FireResult:
        if stage not in {"push", "warn"}:
            raise ValueError(f"unknown idle stage: {stage}")
        if stretch < 1:
            raise ValueError("stretch must be greater than zero")
        if anchor < 1:
            raise ValueError("anchor must be greater than zero")
        policy = resolve_policy(harness, self.config, self.env)
        guard_facts = GuardFacts(
            stretch=stretch,
            anchor=anchor,
            role=self.role,
            harness_enabled=policy.enabled,
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
                return current.model_copy(update={"done": done}), (result, None)

            if current is None:  # pragma: no cover - guarded by decide()
                raise RuntimeError("idle stage cannot be claimed without state")
            done = dict(current.done)
            done[stage] = "sent"
            claimed_state = current.model_copy(update={"done": done})
            return claimed_state, (result, claimed_state)

        result, claimed_state = self.store.mutate(harness, session, transition)
        if result.decision == "act" and claimed_state is not None:
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
            report = self._notify_sender(STAGE_EVENTS[stage], notice)
            for warning in report.warnings:
                logger.warning("idle notification delivery warning: %s", warning)
            ok = report.ok
        except Exception:
            ok = False
        if ok and stage == "push":
            self._clear_push_owed(harness, session, stretch)
        elif not ok:
            self._mark_notification_failed(harness, session, stretch, stage)

    def _emit(
        self, event: HookEventName, notification: Notification
    ) -> RouteReport:
        return emit(event, notification, project_root=self.project_root, env=self.env)

    def _notify_config(self) -> NotifyConfig:
        return self.config.notify

    def _clear_push_owed(self, harness: str, session: str, stretch: int) -> None:
        def transition(current: IdleState | None) -> tuple[IdleState | None, None]:
            if (
                current is None
                or current.stretch != stretch
                or not current.stretch_open
                or current.done.get("push") != "sent"
                or current.push_owed_since_ms is None
            ):
                return current, None
            return current.model_copy(update={"push_owed_since_ms": None}), None

        self.store.mutate(harness, session, transition)

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
                background_tasks=event.background_tasks,
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
            event = hook.observe(payload, read_session, self.env)
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
    "ConfigResult",
    "FireResult",
    "IdlePolicyConfig",
    "IdleService",
    "NotifySender",
    "ReturnResult",
    "close_on_launch",
    "effective",
    "resolve_policy",
]
