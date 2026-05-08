"""Hook dispatch coordination for lifecycle and work events."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import structlog

from meridian.lib.core.lifecycle import LifecycleEvent, LifecycleHook
from meridian.lib.hooks.builtin import BUILTIN_HOOKS
from meridian.lib.hooks.builtin.base import BuiltinHook
from meridian.lib.hooks.interval import IntervalTracker
from meridian.lib.hooks.registry import HookRegistry
from meridian.lib.hooks.runner import ExternalHookRunner, hooks_dispatch_enabled
from meridian.lib.hooks.types import (
    DEFAULT_FAILURE_POLICY,
    DEFAULT_TIMEOUTS,
    EVENT_CLASS,
    FailurePolicy,
    Hook,
    HookContext,
    HookEventClass,
    HookEventName,
    HookResult,
    SpawnStatus,
)
from meridian.plugin_api import Hook as PluginHook
from meridian.plugin_api import HookContext as PluginHookContext

if TYPE_CHECKING:
    from collections.abc import Mapping


class _HookRegistryLike(Protocol):
    def get_hooks_for_event(self, event: HookEventName) -> list[Hook]:
        ...


class _IntervalTrackerLike(Protocol):
    def should_run(self, hook_name: str, interval: str) -> bool:
        ...

    def mark_run(self, hook_name: str) -> None:
        ...


class _ExternalRunnerLike(Protocol):
    def run(self, hook: Hook, context: HookContext, *, timeout_secs: int) -> HookResult:
        ...

logger = structlog.get_logger(__name__)


class HookDispatcher(LifecycleHook):
    """Coordinate hook execution for lifecycle events."""

    def __init__(
        self,
        project_root: Path,
        runtime_root: Path,
        *,
        registry: _HookRegistryLike | None = None,
        interval_tracker: _IntervalTrackerLike | None = None,
        external_runner: _ExternalRunnerLike | None = None,
        builtin_hooks: Mapping[str, BuiltinHook] | None = None,
    ) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._runtime_root = runtime_root.expanduser().resolve()
        self._registry = registry or HookRegistry(self._project_root)
        self._interval_tracker = interval_tracker or IntervalTracker(self._runtime_root)
        self._external_runner = external_runner or ExternalHookRunner(self._project_root)
        self._builtin_hooks = builtin_hooks or BUILTIN_HOOKS

    def on_event(self, event: LifecycleEvent) -> None:
        """LifecycleHook protocol implementation."""

        self.fire(self._build_context(event))

    def fire(self, context: HookContext) -> list[HookResult]:
        """Execute hooks registered for one event and return per-hook results."""

        if not hooks_dispatch_enabled():
            logger.info(
                "hook_dispatch_skipped",
                hook_event=context.event_name,
                event_id=str(context.event_id),
                reason="hooks_disabled",
            )
            return []

        hooks = self._registry.get_hooks_for_event(context.event_name)
        logger.info(
            "hook_dispatch_started",
            hook_event=context.event_name,
            event_id=str(context.event_id),
            hook_count=len(hooks),
        )

        event_class = EVENT_CLASS.get(context.event_name, "observe")
        results: list[HookResult] = []
        for hook in hooks:
            logger.info(
                "hook_execution_started",
                hook=hook.name,
                hook_event=context.event_name,
            )

            result = self._execute_one(hook, context)
            results.append(result)

            if result.skipped:
                logger.info(
                    "hook_execution_skipped",
                    hook=hook.name,
                    hook_event=context.event_name,
                    reason=result.skip_reason,
                )
                continue

            if result.success:
                logger.info(
                    "hook_execution_finished",
                    hook=hook.name,
                    hook_event=context.event_name,
                    outcome=result.outcome,
                    duration_ms=result.duration_ms,
                    exit_code=result.exit_code,
                )
                continue

            fail_open = self._is_fail_open(event_class, self._resolve_failure_policy(hook))
            logger.warning(
                "hook_execution_failed",
                hook=hook.name,
                hook_event=context.event_name,
                outcome=result.outcome,
                error=result.error,
                error_type=self._error_type(result),
                exit_code=result.exit_code,
                fail_open=fail_open,
            )
            if not fail_open:
                logger.warning(
                    "hook_dispatch_stopped_on_failure",
                    hook_event=context.event_name,
                    failed_hook=hook.name,
                )
                break

        logger.info(
            "hook_dispatch_finished",
            hook_event=context.event_name,
            event_id=str(context.event_id),
            result_count=len(results),
        )
        return results

    def _error_type(self, result: HookResult) -> str:
        if result.outcome == "timeout":
            return "timeout"
        if result.exit_code is not None:
            return "process_exit"
        return "execution_error"

    def _execute_one(self, hook: Hook, context: HookContext) -> HookResult:
        if hook.event != context.event_name:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="event_mismatch",
            )

        if not hook.enabled:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="disabled",
            )

        if not self._check_conditions(hook, context):
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="condition_not_met",
            )

        if hook.interval and not self._interval_tracker.should_run(hook.name, hook.interval):
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="throttled",
            )

        timeout_secs = hook.timeout_secs or DEFAULT_TIMEOUTS[EVENT_CLASS[context.event_name]]
        try:
            if hook.builtin:
                result = self._run_builtin(hook, context)
            else:
                result = self._external_runner.run(hook, context, timeout_secs=timeout_secs)
        except Exception as exc:
            result = HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error=str(exc),
            )

        if hook.interval and result.success and not result.skipped:
            self._interval_tracker.mark_run(hook.name)

        return result

    def _run_builtin(self, hook: Hook, context: HookContext) -> HookResult:
        if not hook.builtin:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error="Builtin hook name is required.",
            )

        builtin = self._builtin_hooks.get(hook.builtin)
        if builtin is None:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error=f"Unknown builtin hook: {hook.builtin}",
            )

        requirements_ok, requirements_error = builtin.check_requirements()
        if not requirements_ok:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="requirements",
                error=requirements_error,
            )

        result = builtin.execute(_to_plugin_context(context), _to_plugin_hook(hook))
        return HookResult(
            hook_name=hook.name,
            event=context.event_name,
            outcome=result.outcome,
            success=result.success,
            skipped=result.skipped,
            skip_reason=result.skip_reason,
            error=result.error,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _check_conditions(self, hook: Hook, context: HookContext) -> bool:
        when = hook.when
        if when is None:
            return True

        if when.status and context.spawn_status not in when.status:
            return False

        return not when.agent or context.spawn_agent == when.agent

    def _resolve_failure_policy(self, hook: Hook) -> FailurePolicy:
        if hook.failure_policy is not None:
            return hook.failure_policy
        event_class: HookEventClass = EVENT_CLASS[hook.event]
        return DEFAULT_FAILURE_POLICY[event_class]

    def _is_fail_open(self, event_class: HookEventClass, policy: FailurePolicy) -> bool:
        if event_class in ("observe", "post"):
            return True
        return policy != "fail"

    def _build_context(self, event: LifecycleEvent) -> HookContext:
        return HookContext.from_roots(
            project_root=self._project_root,
            runtime_root=self._runtime_root,
            event_name=cast("HookEventName", event.event_type),
            event_id=event.event_id,
            timestamp=event.timestamp.isoformat(),
            spawn_id=event.spawn_id,
            spawn_status=_normalize_spawn_status(event.status),
            spawn_agent=event.agent,
            spawn_model=event.model,
            spawn_duration_secs=event.duration_secs,
            spawn_cost_usd=event.total_cost_usd,
            work_id=event.work_id,
        )


def _normalize_spawn_status(value: str | None) -> SpawnStatus | None:
    if value == "succeeded":
        return "success"
    if value == "failed":
        return "failure"
    if value in ("success", "failure", "cancelled", "timeout", "skipped"):
        return value
    return None


def _to_plugin_hook(hook: Hook) -> PluginHook:
    return PluginHook(
        name=hook.name,
        event=hook.event,
        source=hook.source,
        builtin=hook.builtin,
        command=hook.command,
        enabled=hook.enabled,
        priority=hook.priority,
        require_serial=hook.require_serial,
        exclude=hook.exclude,
        options=hook.options,
        failure_policy=hook.failure_policy,
        remote=hook.remote,
    )


def _to_plugin_context(context: HookContext) -> PluginHookContext:
    return PluginHookContext(
        event_name=context.event_name,
        event_id=context.event_id,
        timestamp=context.timestamp,
        project_root=context.project_root,
        runtime_root=context.runtime_root,
        schema_version=context.schema_version,
        spawn_id=context.spawn_id,
        spawn_status=context.spawn_status,
        spawn_agent=context.spawn_agent,
        spawn_model=context.spawn_model,
        spawn_duration_secs=context.spawn_duration_secs,
        spawn_cost_usd=context.spawn_cost_usd,
        spawn_error=context.spawn_error,
        work_id=context.work_id,
        work_dir=context.work_dir,
    )
