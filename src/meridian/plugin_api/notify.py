"""Plugin-facing delivery through Meridian's configured notification transports."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.config.loader import load_config
from meridian.lib.notify.notice import Notice, SessionLabel
from meridian.lib.notify.service import DEFAULT_CHANNELS, send
from meridian.plugin_api.types import HookEventName, Notification

REPORT_KEY = "notify_report"


@dataclass(frozen=True)
class Delivery:
    """What one delivery did, per transport."""

    ok: bool
    # Failed transports as `channel: error`, empty when all delivered.
    errors: tuple[str, ...]
    # The same report `meridian notify --json` prints, keyed for hook stdout.
    report_json: str


def deliver(
    notification: Notification,
    *,
    event: HookEventName,
    project_root: str | Path,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    priority: int | None = None,
) -> Delivery:
    """Send one notification through the transports the project configures.

    `channels` takes `push` and `email` (the configured backends; `email` only
    when the notification wants email) or a backend name.
    """

    notice = Notice(
        title=notification.title,
        body=notification.body,
        priority=priority if priority is not None else notification.priority,
        email=notification.email,
        event=event,
        label=SessionLabel.from_dict(notification.label),
        attachments=tuple(Path(path) for path in notification.attachments),
    )
    report = send(notice, load_config(Path(project_root)).notify, channels)
    errors = tuple(
        f"{result.channel}: {result.error or 'delivery failed'}"
        for result in report.results
        if result.status == "failed"
    )
    return Delivery(
        ok=report.ok,
        errors=errors,
        report_json=json.dumps({REPORT_KEY: report.to_dict()}),
    )


__all__ = ["REPORT_KEY", "Delivery", "deliver"]
