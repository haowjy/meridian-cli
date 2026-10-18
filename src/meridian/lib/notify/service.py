"""Notification fan-out policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels import resolve_channel
from meridian.lib.notify.channels.base import SendResult
from meridian.plugin_api.types import Notification

_ALL_NONE_WARNING = "notification disabled: every selected backend is none"
# The configured push backend, and the email backend when the notification wants email.
DEFAULT_CHANNELS: tuple[str, ...] = ("push", "email")


@dataclass(frozen=True)
class SendReport:
    """Aggregate result from one transport fan-out."""

    results: tuple[SendResult, ...]
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


def _backends(notification: Notification, cfg: NotifyConfig, channels: Sequence[str]) -> list[str]:
    aliases = {"push": cfg.push_backend, "email": cfg.email_backend}
    return [
        aliases.get(channel, channel)
        for channel in channels
        if channel != "email" or notification.email
    ]


def send(
    notification: Notification,
    cfg: NotifyConfig,
    *,
    event: str,
    channels: Sequence[str] | None = None,
) -> SendReport:
    """Fan out once to each selected backend; never retry in-process.

    `push` and `email` name the configured backends, and unset means both; any
    other entry names a backend directly (`ntfy`, `smtp`, `gmail`, `none`).
    Email backends skip a notification that does not want email, however named.
    """

    results: list[SendResult] = []
    for backend in dict.fromkeys(
        _backends(notification, cfg, DEFAULT_CHANNELS if channels is None else channels)
    ):
        try:
            channel = resolve_channel(backend)
            if channel.email and not notification.email:
                continue
            result = channel.send(notification, cfg, event=event)
        except Exception as exc:  # channel boundary: one failure must not block another
            result = SendResult(channel=backend, status="failed", error=str(exc))
        results.append(result)

    warnings = tuple(result.warning for result in results if result.warning is not None)
    report = SendReport(results=tuple(results), warnings=warnings)
    if results and report.all_none:
        report = replace(report, warnings=(*report.warnings, _ALL_NONE_WARNING))
    return report


__all__ = ["DEFAULT_CHANNELS", "SendReport", "send"]
