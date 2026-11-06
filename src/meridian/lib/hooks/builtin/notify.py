"""Built-in notification delivery: routes a notification event to transports."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

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

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None

        from meridian.lib.notify.channels import normalize_channel_name

        return tuple(
            normalize_channel_name(channel, aliases=("push", "email")) for channel in value
        )


class NotifyHook:
    """Deliver the event's notification to the channels its route names.

    The transports are `lib/notify`'s, a companion this built-in wraps directly.
    """

    name = "notify"
    requirements: tuple[str, ...] = ()
    default_events: tuple[HookEventName, ...] = NOTIFICATION_EVENTS
    supported_events: tuple[HookEventName, ...] | None = NOTIFICATION_EVENTS
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
        priority = cast("int | None", config.options.get("priority"))
        channels = cast("tuple[str, ...] | None", config.options.get("channels"))
        if priority is not None:
            notification = replace(notification, priority=priority)
        report = send(
            notification,
            load_config(Path(context.project_root)).notify,
            event=context.event_name,
            channels=channels,
        )
        if report.all_none:
            skipped = self._skipped(context, config, "disabled")
            return replace(skipped, warning="; ".join(report.warnings) or None)
        diagnostics = list(report.warnings)
        diagnostics.extend(
            f"{result.channel}: {result.error or 'delivery failed'}"
            for result in report.results
            if result.status == "failed"
        )
        diagnostic = "; ".join(diagnostics) or None
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="success" if report.ok else "failure",
            success=report.ok,
            warning=diagnostic if report.ok else None,
            error=None if report.ok else diagnostic,
            duration_ms=int((time.monotonic() - start) * 1000),
            details={"transports": tuple(result.channel for result in report.results if result.ok)},
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
