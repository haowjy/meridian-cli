"""Emit a notification as its hook event; the routes on that event deliver it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.service import SendReport

if TYPE_CHECKING:
    from meridian.plugin_api.types import HookEventName, HookResult, Notification

_NO_ROUTE_WARNING = "no notification route ran for {event}"
logger = structlog.get_logger(__name__)


def _runtime_root(project_root: Path) -> Path | None:
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    try:
        return resolve_project_runtime_root_or_none(project_root)
    except (OSError, ValueError):
        return None


def _route_result(result: HookResult) -> SendResult:
    if result.skipped:
        # A route that chose not to deliver; its error says why, when it knows.
        return SendResult(channel=result.hook_name, status="none", warning=result.error)
    if result.success:
        # Delivered somewhere; an error here is a transport the route also named that failed.
        return SendResult(channel=result.hook_name, status="sent", warning=result.error)
    return SendResult(
        channel=result.hook_name,
        status="failed",
        error=result.error or result.outcome,
    )


def report_from_hooks(event: str, hook_results: Sequence[HookResult]) -> SendReport:
    """One result per route; with no route at all, ok with a warning."""

    results = tuple(_route_result(result) for result in hook_results)
    warnings = tuple(
        f"{result.channel}: {result.warning}" for result in results if result.warning
    )
    if not results:
        warnings = (_NO_ROUTE_WARNING.format(event=event),)
    return SendReport(results=results, warnings=warnings)


def emit(
    event: HookEventName,
    notification: Notification,
    *,
    project_root: Path,
    env: Mapping[str, str],
) -> SendReport:
    """Fire the notification's hook event and report what its routes delivered."""

    from meridian.lib.hooks.config import default_hooks_config
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.hooks.registry import HookRegistry
    from meridian.lib.hooks.types import HookContext

    runtime_root = _runtime_root(project_root)
    context = HookContext.from_roots(
        project_root=project_root,
        runtime_root=runtime_root,
        event_name=event,
        event_id=uuid4(),
        timestamp=datetime.now(UTC).isoformat(),
        spawn_id=env.get("MERIDIAN_SPAWN_ID") or None,
        spawn_agent=notification.label.get("agent"),
        work_id=notification.label.get("work_id"),
        notification=notification,
    )
    config_warning: str | None = None
    try:
        dispatcher = HookDispatcher(project_root, runtime_root, env=env)
    except (OSError, ValueError) as exc:
        config_warning = f"invalid hook configuration; using default notification routes: {exc}"
        logger.warning(
            "notification_hook_config_invalid",
            hook_event=event,
            error=str(exc),
        )
        dispatcher = HookDispatcher(
            project_root,
            runtime_root,
            env=env,
            registry=HookRegistry(
                project_root,
                hooks_config=default_hooks_config(),
            ),
        )

    report = report_from_hooks(event, dispatcher.fire(context))
    if config_warning is not None:
        return SendReport(
            results=report.results,
            warnings=(config_warning, *report.warnings),
        )
    return report


__all__ = ["emit", "report_from_hooks"]
