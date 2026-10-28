"""Emit a notification as its hook event; the routes on that event deliver it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from meridian.lib.notify.channels.base import SendStatus
from meridian.lib.utils.json_shapes import is_object_sequence

if TYPE_CHECKING:
    from meridian.plugin_api.types import HookEventName, HookResult, Notification

_NO_ROUTE_WARNING = "no notification route ran for {event}"


@dataclass(frozen=True)
class RouteResult:
    """Outcome from one hook route, including transports it reached."""

    route: str
    status: SendStatus
    error: str | None = None
    warning: str | None = None
    transports: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "sent"

    def to_dict(self) -> dict[str, object]:
        # `channel` is the established CLI wire name for a notification route.
        return {
            "channel": self.route,
            "status": self.status,
            "ok": self.ok,
            "error": self.error,
            "warning": self.warning,
            "transports": list(self.transports),
        }


@dataclass(frozen=True)
class RouteReport:
    """Aggregate result from notification hook routes."""

    results: tuple[RouteResult, ...]
    warnings: tuple[str, ...] = ()

    @property
    def all_none(self) -> bool:
        return all(result.status == "none" for result in self.results)

    @property
    def ok(self) -> bool:
        """Something delivered, or nothing was meant to (no route, or every one off)."""

        return self.all_none or any(result.ok for result in self.results)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "results": [result.to_dict() for result in self.results],
            "warnings": list(self.warnings),
        }


def _runtime_root(project_root: Path) -> Path | None:
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    try:
        return resolve_project_runtime_root_or_none(project_root)
    except (OSError, ValueError):
        return None


def _transports(result: HookResult) -> tuple[str, ...]:
    if "transports" not in result.details:
        return ()
    raw = result.details["transports"]
    if not is_object_sequence(raw):
        raise TypeError("HookResult.details['transports'] must be a sequence of strings")
    transports: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise TypeError("HookResult.details['transports'] must be a sequence of strings")
        transports.append(value)
    return tuple(transports)


def _route_result(result: HookResult) -> RouteResult:
    transports = _transports(result)
    if result.skipped:
        return RouteResult(
            route=result.hook_name,
            status="none",
            warning=result.warning or result.error,
            transports=transports,
        )
    if result.success:
        return RouteResult(
            route=result.hook_name,
            status="sent",
            warning=result.warning or result.error,
            transports=transports,
        )
    return RouteResult(
        route=result.hook_name,
        status="failed",
        error=result.error or result.outcome,
    )


def report_from_hooks(
    event: str, hook_results: Sequence[HookResult]
) -> RouteReport:
    """One result per route; with no route at all, ok with a warning."""

    results = tuple(_route_result(result) for result in hook_results)
    warnings = tuple(
        f"{result.route}: {result.warning}" for result in results if result.warning
    )
    if not results:
        warnings = (_NO_ROUTE_WARNING.format(event=event),)
    return RouteReport(results=results, warnings=warnings)


def emit(
    event: HookEventName,
    notification: Notification,
    *,
    project_root: Path,
    env: Mapping[str, str],
) -> RouteReport:
    """Fire the notification's hook event and report what its routes delivered."""

    from meridian.lib.hooks.config import load_hooks_config_report
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.hooks.registry import HookRegistry
    from meridian.lib.hooks.types import HookContext

    runtime_root = _runtime_root(project_root)
    spawn_id = env.get("MERIDIAN_SPAWN_ID") or None
    spawn_kind = None
    if runtime_root is not None and spawn_id is not None:
        from meridian.lib.state import spawn_store

        spawn = spawn_store.get_spawn(runtime_root, spawn_id)
        spawn_kind = spawn.kind if spawn is not None else None
    context = HookContext.from_roots(
        project_root=project_root,
        runtime_root=runtime_root,
        event_name=event,
        event_id=uuid4(),
        timestamp=datetime.now(UTC).isoformat(),
        spawn_id=spawn_id,
        spawn_agent=notification.label.get("agent"),
        spawn_kind=spawn_kind,
        work_id=notification.label.get("work_id"),
        notification=notification,
    )
    loaded_hooks = load_hooks_config_report(project_root)
    dispatcher = HookDispatcher(
        project_root,
        runtime_root,
        env=env,
        registry=HookRegistry(
            project_root,
            hooks_config=loaded_hooks.config,
        ),
    )

    report = report_from_hooks(event, dispatcher.fire(context))
    if loaded_hooks.errors:
        dropped = "dropped invalid hook rows: " + "; ".join(loaded_hooks.errors)
        report = replace(report, warnings=(*report.warnings, dropped))
    return report


__all__ = ["RouteReport", "RouteResult", "emit", "report_from_hooks"]
