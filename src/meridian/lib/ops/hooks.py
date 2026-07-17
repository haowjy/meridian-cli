"""Hook operations exposed via CLI manifest."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.util import FormatContext
from meridian.lib.hooks.builtin import BUILTIN_HOOKS
from meridian.lib.hooks.dispatch import HookDispatcher
from meridian.lib.hooks.interval import IntervalTracker
from meridian.lib.hooks.registry import HookRegistry
from meridian.lib.hooks.types import Hook, HookContext, HookEventName, HookResult
from meridian.lib.ops.runtime import resolve_roots, resolve_roots_for_read


class HookListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class HookListItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    event: HookEventName
    hook_type: str
    source: str
    status: str
    auto_registered: bool = False
    registration: str = "config"


class HookListOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    hooks: tuple[HookListItem, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.hooks:
            return "(no hooks configured)"

        from meridian.lib.core.formatting import tabular

        rows = [["name", "event", "type", "source", "registration", "status"]]
        rows.extend(
            [
                item.name,
                item.event,
                item.hook_type,
                item.source,
                item.registration,
                item.status,
            ]
            for item in self.hooks
        )
        return tabular(rows)


class HookCheckInput(BaseModel):
    model_config = ConfigDict(frozen=True)


class HookCheckItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    requirements: tuple[str, ...] = ()
    error: str | None = None


class HookCheckOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    checks: tuple[HookCheckItem, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.checks:
            return "(no builtin hooks registered)"

        from meridian.lib.core.formatting import tabular

        rows = [["name", "status", "requirements", "error"]]
        for item in self.checks:
            rows.append(
                [
                    item.name,
                    "ok" if item.ok else "missing",
                    ", ".join(item.requirements) if item.requirements else "-",
                    item.error or "-",
                ]
            )
        return tabular(rows)


class HookRunInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    event: HookEventName | None = None
    project_root: str | None = None


class HookRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    success: bool
    skipped: bool
    skip_reason: str | None = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int
    stdout: str | None = None
    stderr: str | None = None


class HookRunOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    hook: str
    event: HookEventName
    result: HookRunResult

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx

        from meridian.lib.core.formatting import kv_block

        return kv_block(
            [
                ("hook", self.hook),
                ("event", self.event),
                ("outcome", self.result.outcome),
                ("success", str(self.result.success).lower()),
                ("skipped", str(self.result.skipped).lower()),
                ("skip_reason", self.result.skip_reason or "-"),
                (
                    "exit_code",
                    str(self.result.exit_code) if self.result.exit_code is not None else "-",
                ),
                ("duration_ms", str(self.result.duration_ms)),
                ("error", self.result.error or "-"),
            ]
        )


class HookResolveInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: HookEventName
    project_root: str | None = None


class HookResolveItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    source: str
    event: HookEventName
    hook_type: str
    priority: int
    interval: str | None = None
    when_status: tuple[str, ...] | None = None
    when_agent: str | None = None


class HookResolveOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: HookEventName
    hooks: tuple[HookResolveItem, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.hooks:
            return f"(no hooks for event '{self.event}')"

        from meridian.lib.core.formatting import tabular

        rows = [["name", "source", "type", "priority", "interval", "when"]]
        for item in self.hooks:
            when_parts: list[str] = []
            if item.when_status:
                when_parts.append("status=" + ",".join(item.when_status))
            if item.when_agent:
                when_parts.append(f"agent={item.when_agent}")
            rows.append(
                [
                    item.name,
                    item.source,
                    item.hook_type,
                    str(item.priority),
                    item.interval or "-",
                    "; ".join(when_parts) if when_parts else "-",
                ]
            )
        return tabular(rows)


class _SingleHookRegistry:
    def __init__(self, hook: Hook) -> None:
        self._hook = hook

    def get_hooks_for_event(self, event: HookEventName) -> list[Hook]:
        if event != self._hook.event:
            return []
        return [self._hook]


class _BypassIntervalTracker:
    def __init__(self, delegate: IntervalTracker) -> None:
        self._delegate = delegate

    def run_if_due(
        self,
        hook_name: str,
        interval: str,
        fn: Callable[[], HookResult],
    ) -> HookResult | None:
        _ = interval
        return self._delegate.run_if_due(hook_name, None, fn)


def _hook_type(hook: Hook) -> str:
    return "builtin" if hook.builtin else "external"


def _hook_status(hook: Hook) -> str:
    return "enabled" if hook.enabled else "disabled"


def hooks_list_sync(payload: HookListInput) -> HookListOutput:
    roots = resolve_roots_for_read(payload.project_root)
    registry = HookRegistry(roots.project_root)
    hooks = tuple(
        HookListItem(
            name=hook.name,
            event=hook.event,
            hook_type=_hook_type(hook),
            source=hook.source,
            auto_registered=hook.auto_registered,
            registration="auto" if hook.auto_registered else "config",
            status=_hook_status(hook),
        )
        for hook in registry.get_all_hooks()
    )
    return HookListOutput(hooks=hooks)


def hooks_check_sync(payload: HookCheckInput) -> HookCheckOutput:
    _ = payload
    checks: list[HookCheckItem] = []
    all_ok = True
    for name in sorted(BUILTIN_HOOKS):
        implementation = BUILTIN_HOOKS[name]
        ok, error = implementation.check_requirements()
        all_ok = all_ok and ok
        checks.append(
            HookCheckItem(
                name=name,
                ok=ok,
                requirements=tuple(getattr(implementation, "requirements", ())),
                error=error,
            )
        )

    return HookCheckOutput(ok=all_ok, checks=tuple(checks))


def _manual_context(
    *,
    hook: Hook,
    event: HookEventName,
    project_root: Path,
    runtime_root: Path,
) -> HookContext:
    when = hook.when
    spawn_status = when.status[0] if when and when.status else "success"
    spawn_agent = when.agent if when and when.agent else "manual"

    base = HookContext(
        event_name=event,
        event_id=uuid4(),
        timestamp=datetime.now(UTC).isoformat(),
        project_root=project_root.as_posix(),
        runtime_root=runtime_root.as_posix(),
        spawn_id="manual",
        spawn_status=spawn_status,
        spawn_agent=spawn_agent,
        spawn_model="manual",
    )

    if event.startswith("work."):
        return replace(
            base,
            work_id="manual",
            work_dir=project_root.as_posix(),
        )

    return base


def _result_to_output(result: HookResult) -> HookRunResult:
    return HookRunResult(
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


def hooks_run_sync(payload: HookRunInput) -> HookRunOutput:
    roots = resolve_roots(payload.project_root)
    registry = HookRegistry(roots.project_root)

    hook_name = payload.name.strip()
    if not hook_name:
        raise ValueError("Hook name is required.")

    hook = registry.get_hook(hook_name)
    if hook is None:
        raise ValueError(f"Hook '{hook_name}' not found")

    effective_hook = hook if hook.enabled else replace(hook, enabled=True)
    run_event = payload.event or effective_hook.event
    if run_event != effective_hook.event:
        effective_hook = replace(effective_hook, event=run_event)

    dispatcher = HookDispatcher(
        roots.project_root,
        roots.runtime_root,
        registry=_SingleHookRegistry(effective_hook),
        interval_tracker=_BypassIntervalTracker(IntervalTracker(roots.runtime_root)),
    )
    context = _manual_context(
        hook=effective_hook,
        event=run_event,
        project_root=roots.project_root,
        runtime_root=roots.runtime_root,
    )

    results = dispatcher.fire(context)
    if not results:
        raise RuntimeError(f"Hook '{hook_name}' produced no execution result")

    result = results[0]
    return HookRunOutput(
        hook=effective_hook.name,
        event=effective_hook.event,
        result=_result_to_output(result),
    )


def hooks_resolve_sync(payload: HookResolveInput) -> HookResolveOutput:
    roots = resolve_roots_for_read(payload.project_root)
    registry = HookRegistry(roots.project_root)

    hooks = tuple(
        HookResolveItem(
            name=hook.name,
            source=hook.source,
            event=hook.event,
            hook_type=_hook_type(hook),
            priority=hook.priority,
            interval=hook.interval,
            when_status=tuple(hook.when.status) if hook.when and hook.when.status else None,
            when_agent=hook.when.agent if hook.when else None,
        )
        for hook in registry.get_hooks_for_event(payload.event)
    )
    return HookResolveOutput(event=payload.event, hooks=hooks)


async def hooks_list(payload: HookListInput) -> HookListOutput:
    return await asyncio.to_thread(hooks_list_sync, payload)


async def hooks_check(payload: HookCheckInput) -> HookCheckOutput:
    return await asyncio.to_thread(hooks_check_sync, payload)


async def hooks_run(payload: HookRunInput) -> HookRunOutput:
    return await asyncio.to_thread(hooks_run_sync, payload)


async def hooks_resolve(payload: HookResolveInput) -> HookResolveOutput:
    return await asyncio.to_thread(hooks_resolve_sync, payload)


__all__ = [
    "HookCheckInput",
    "HookCheckItem",
    "HookCheckOutput",
    "HookListInput",
    "HookListItem",
    "HookListOutput",
    "HookResolveInput",
    "HookResolveItem",
    "HookResolveOutput",
    "HookRunInput",
    "HookRunOutput",
    "HookRunResult",
    "hooks_check",
    "hooks_check_sync",
    "hooks_list",
    "hooks_list_sync",
    "hooks_resolve",
    "hooks_resolve_sync",
    "hooks_run",
    "hooks_run_sync",
]
