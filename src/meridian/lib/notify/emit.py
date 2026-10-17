"""Emit a notice as its hook event; the routes on that event deliver it."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from meridian.lib.notify.channels.base import SendResult, SendStatus
from meridian.lib.notify.notice import Notice
from meridian.lib.notify.service import SendReport
from meridian.lib.utils.json_shapes import json_list, json_object

if TYPE_CHECKING:
    from meridian.lib.hooks.types import HookResult

_NO_ROUTE_WARNING = "no notification route ran for {event}"


def _runtime_root(project_root: Path) -> Path | None:
    from meridian.lib.state.paths import resolve_project_runtime_root_or_none

    try:
        return resolve_project_runtime_root_or_none(project_root)
    except (OSError, ValueError):
        return None


def _channel_report(stdout: str | None) -> SendReport | None:
    """The per-transport report a built-in `notify` route printed, if this is one."""

    from meridian.plugin_api.notify import REPORT_KEY

    if not stdout:
        return None
    try:
        payload = json_object(json.loads(stdout))
    except ValueError:
        return None
    report = json_object(payload.get(REPORT_KEY)) if payload is not None else None
    if report is None:
        return None
    rows = json_list(report.get("results"))
    if rows is None:
        return None
    results: list[SendResult] = []
    for raw in rows:
        row = json_object(raw)
        if row is None:
            continue
        status = row.get("status")
        if status not in {"sent", "failed", "none"}:
            status = "failed"
        error = row.get("error")
        warning = row.get("warning")
        results.append(
            SendResult(
                channel=str(row.get("channel")),
                status=cast("SendStatus", status),
                error=error if isinstance(error, str) else None,
                warning=warning if isinstance(warning, str) else None,
            )
        )
    warnings = json_list(report.get("warnings")) or []
    return SendReport(
        results=tuple(results),
        warnings=tuple(warning for warning in warnings if isinstance(warning, str)),
    )


def report_from_hooks(event: str, hook_results: Sequence[HookResult]) -> SendReport:
    """Fold route results into one report: transports for `notify`, `hook:<name>` otherwise."""

    results: list[SendResult] = []
    warnings: list[str] = []
    for result in hook_results:
        if result.skipped:
            continue
        channels = _channel_report(result.stdout)
        if channels is not None:
            results.extend(channels.results)
            warnings.extend(channels.warnings)
            continue
        results.append(
            SendResult(
                channel=f"hook:{result.hook_name}",
                status="sent" if result.success else "failed",
                error=None if result.success else (result.error or result.outcome),
            )
        )
    if not results:
        warnings.append(_NO_ROUTE_WARNING.format(event=event))
    return SendReport(results=tuple(results), warnings=tuple(dict.fromkeys(warnings)))


def emit(notice: Notice, *, project_root: Path, env: Mapping[str, str]) -> SendReport:
    """Fire the notice's hook event and report what its routes delivered."""

    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.hooks.types import HookContext, HookEventName, Notification

    runtime_root = _runtime_root(project_root)
    label = notice.label
    context = HookContext.from_roots(
        project_root=project_root,
        runtime_root=runtime_root,
        event_name=cast("HookEventName", notice.event),
        event_id=uuid4(),
        timestamp=datetime.now(UTC).isoformat(),
        spawn_id=env.get("MERIDIAN_SPAWN_ID") or None,
        spawn_agent=label.agent if label is not None else None,
        work_id=label.work_id if label is not None else None,
        notification=Notification(
            title=notice.title,
            body=notice.body,
            priority=notice.priority,
            email=notice.email,
            label=label.to_dict() if label is not None else {},
            attachments=tuple(path.as_posix() for path in notice.attachments),
        ),
    )
    results = HookDispatcher(project_root, runtime_root).fire(context)
    return report_from_hooks(notice.event, results)


__all__ = ["emit", "report_from_hooks"]
