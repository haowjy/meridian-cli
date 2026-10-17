"""Built-in notification delivery: routes a notification event to transports."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from meridian.plugin_api import (
    NOTIFICATION_EVENTS,
    Hook,
    HookContext,
    HookEventName,
    HookResult,
)

type _ChannelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class NotifyOptions(BaseModel):
    """A route's `options`: where it delivers, and at what priority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # `push`, `email` (the configured backends) or backend names; unset means both aliases.
    channels: tuple[_ChannelName, ...] | None = None
    priority: Annotated[StrictInt, Field(ge=1, le=5)] | None = None


class NotifyHook:
    """Deliver the event's notification to the channels its route names.

    The transports are `lib/notify`'s, a companion this built-in wraps directly.
    """

    name = "notify"
    requirements: tuple[str, ...] = ()
    default_events: tuple[HookEventName, ...] = NOTIFICATION_EVENTS
    default_interval: str | None = None
    options_model: type[BaseModel] = NotifyOptions
    # Unnamed rows are all `notify`, so a row on an event replaces its default route.
    identity_options: tuple[str, ...] = ()

    def check_requirements(self) -> tuple[bool, str | None]:
        return True, None

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        from dataclasses import replace

        from meridian.lib.config.loader import load_config
        from meridian.lib.notify.service import send

        start = time.monotonic()
        notification = context.notification
        if notification is None:
            return self._skipped(context, config, "no_notification")
        options = NotifyOptions.model_validate(config.options)
        if options.priority is not None:
            notification = replace(notification, priority=options.priority)
        report = send(
            notification,
            load_config(Path(context.project_root)).notify,
            event=context.event_name,
            channels=options.channels,
        )
        if report.all_none:
            skipped = self._skipped(context, config, "disabled")
            return replace(skipped, error="; ".join(report.warnings) or None)
        errors = [
            f"{result.channel}: {result.error or 'delivery failed'}"
            for result in report.results
            if result.status == "failed"
        ]
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="success" if report.ok else "failure",
            success=report.ok,
            error="; ".join(errors) or None,
            duration_ms=int((time.monotonic() - start) * 1000),
            stdout=", ".join(result.channel for result in report.results if result.ok) or None,
        )

    def _skipped(self, context: HookContext, config: Hook, reason: str) -> HookResult:
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="skipped",
            success=True,
            skipped=True,
            skip_reason=reason,
        )


NOTIFY = NotifyHook()

__all__ = ["NOTIFY", "NotifyHook", "NotifyOptions"]
