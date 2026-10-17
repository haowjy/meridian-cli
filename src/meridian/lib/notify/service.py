"""Notification fan-out policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from meridian.lib.config.models import NotifyConfig
from meridian.lib.notify.channels import resolve_channel
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice, SessionLabel

_ALL_NONE_WARNING = "notification disabled: every selected backend is none"
# The configured push backend, and the email backend when the notice wants email.
DEFAULT_CHANNELS: tuple[str, ...] = ("push", "email")


@dataclass(frozen=True)
class SendReport:
    """Aggregate delivery result used by CLI and idle callers."""

    results: tuple[SendResult, ...]
    warnings: tuple[str, ...] = ()
    label: SessionLabel | None = None

    @property
    def all_none(self) -> bool:
        return bool(self.results) and all(result.status == "none" for result in self.results)

    @property
    def ok(self) -> bool:
        """Something delivered, or nothing was meant to (every route off or none)."""

        return (
            not self.results
            or any(result.ok for result in self.results)
            or self.all_none
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def with_label(self, label: SessionLabel) -> SendReport:
        return replace(self, label=label)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "label": self.label.to_dict() if self.label is not None else None,
            "results": [result.to_dict() for result in self.results],
            "warnings": list(self.warnings),
        }


def _backends(notice: Notice, cfg: NotifyConfig, channels: Sequence[str]) -> list[str]:
    backends: list[str] = []
    for channel in channels:
        name = channel.strip().lower()
        if name == "push":
            backends.append(cfg.push_backend)
        elif name == "email":
            if notice.email:
                backends.append(cfg.email_backend)
        else:
            backends.append(name)
    return backends


def send(
    notice: Notice,
    cfg: NotifyConfig,
    channels: Sequence[str] = DEFAULT_CHANNELS,
) -> SendReport:
    """Fan out once to each selected backend; never retry in-process.

    `push` and `email` name the configured backends; any other entry names a
    backend directly (`ntfy`, `smtp`, `gmail`, `command`, `none`).
    """
    backends = _backends(notice, cfg, channels)

    results: list[SendResult] = []
    for backend in dict.fromkeys(backends):
        try:
            channel = resolve_channel(backend)
            result = channel.send(notice, cfg)
        except Exception as exc:  # channel boundary: one failure must not block another
            result = SendResult(channel=backend, status="failed", error=str(exc))
        results.append(result)

    warnings = tuple(result.warning for result in results if result.warning is not None)
    report = SendReport(results=tuple(results), warnings=warnings)
    if report.all_none:
        report = replace(report, warnings=(*report.warnings, _ALL_NONE_WARNING))
    return report


__all__ = ["DEFAULT_CHANNELS", "SendReport", "send"]
