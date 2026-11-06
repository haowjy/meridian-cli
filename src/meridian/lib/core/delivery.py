"""How far one user message got into a live harness session.

Stdlib only, in ``lib/core``: every layer that routes a message (connections, the
control-action coordinator, the control socket, the CLI) imports it without running
``harness/__init__``.
"""

from __future__ import annotations

from enum import StrEnum


class DeliveryKind(StrEnum):
    """What the harness confirmed when Meridian handed it a message."""

    POSTED = "posted"
    """Written to the harness; it sends no receipt, so the message may still be dropped."""

    ACKNOWLEDGED = "acknowledged"
    """The harness answered the request that carried the message."""


__all__ = ["DeliveryKind"]
