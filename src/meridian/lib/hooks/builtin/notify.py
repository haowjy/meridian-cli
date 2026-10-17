"""Built-in notification delivery: routes a notification event to transports."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import cast

from meridian.plugin_api import (
    NOTIFICATION_EVENTS,
    Hook,
    HookContext,
    HookResult,
)
from meridian.plugin_api.notify import deliver


class NotifyHook:
    """Deliver the event's notification to the channels its route names."""

    name = "notify"
    requirements: tuple[str, ...] = ()

    @property
    def default_events(self) -> tuple[str, ...]:
        return NOTIFICATION_EVENTS

    @property
    def default_interval(self) -> str | None:
        return None

    def check_requirements(self) -> tuple[bool, str | None]:
        return True, None

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        start = time.monotonic()
        notification = context.notification
        if notification is None:
            return HookResult(
                hook_name=config.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="no_notification",
            )
        channels = config.options.get("channels")
        priority = config.options.get("priority")
        delivery = deliver(
            notification,
            event=context.event_name,
            project_root=context.project_root,
            # Option shapes are checked when the hook row loads.
            channels=cast("Sequence[str]", channels) if channels is not None else ("push", "email"),
            priority=cast("int", priority) if priority is not None else None,
        )
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="success" if delivery.ok else "failure",
            success=delivery.ok,
            error="; ".join(delivery.errors) or None,
            duration_ms=int((time.monotonic() - start) * 1000),
            stdout=delivery.report_json,
        )


NOTIFY = NotifyHook()

__all__ = ["NOTIFY", "NotifyHook"]
