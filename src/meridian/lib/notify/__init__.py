"""Harness-agnostic notification delivery."""

from meridian.lib.notify.emit import RouteReport, RouteResult
from meridian.lib.notify.notice import SessionLabel, manual_notification

# Package callers consume route-level reports. The idle sender seam still uses
# this package-level spelling; transport reports stay local to `service`.
SendReport = RouteReport

__all__ = [
    "RouteReport",
    "RouteResult",
    "SendReport",
    "SessionLabel",
    "manual_notification",
]
