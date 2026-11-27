"""How far one user message got into a live harness session.

Stdlib only, in ``lib/core``: every layer that routes a message (connections, the
control-action coordinator, the control socket, the CLI) imports it without running
``harness/__init__``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class DeliveryKind(StrEnum):
    """What the harness confirmed when Meridian handed it a message."""

    POSTED = "posted"
    """Written to the harness; it sends no receipt, so the message may still be dropped."""

    ACKNOWLEDGED = "acknowledged"
    """The harness answered the request that carried the message."""

    QUEUED = "queued"
    """Held for a harness that pulls: the session reads it at its next model call, which
    may be never if it ends first. The sender is told then."""


class DeliveryError(RuntimeError):
    """A message was not confirmed delivered; it may have arrived unless a subclass says not."""


class DeliveryRefused(DeliveryError):
    """The adapter asserts the message was not delivered, so retrying is safe."""


class TransportDead(DeliveryError):
    """The receiver is gone and nothing was written."""


TUI_MOVED_MESSAGE = (
    "The TUI moved to another session, and a primary takes messages only for the session "
    "it started with. Nothing was sent. Type in the TUI, or relaunch with "
    "`meridian --continue <chat>` (it reopens the first session, not the one the TUI is on) "
    "and send again."
)
"""Refusal text once a Codex or OpenCode primary's TUI has left the session the route serves."""

SEND_TIMEOUT_SECONDS = 33.0
"""How long one send may run. Above the harnesses' own 30 s request timeouts, whose
failure text should win, and below the CLI's wait for the reply (this plus 2 s)."""

Deliver = Callable[[str], Awaitable[DeliveryKind]]


@dataclass(frozen=True)
class MessageSender:
    """The session that sent a message: its spawn and the project that spawn lives in.

    A sender on another machine also names that machine by ID, and the ID to answer
    (``reply_id``; ``claimed`` when it is machine-wide): ``spawn_id`` and ``project_root`` are
    then paths on that machine.
    """

    spawn_id: str
    project_root: str
    machine_id: str | None = None
    reply_id: str | None = None
    claimed: bool = False


RouteDeliver = Callable[[str, MessageSender | None], Awaitable[DeliveryKind]]


@dataclass(frozen=True)
class MessageRoute:
    """Where one session's messages go: ``deliver`` raises ``DeliveryError`` on failure.

    ``deliver`` gets the sender as data, ``None`` when the message has no identity; a route
    that tells the sender about a later failure needs it, and the text frame is for the model.
    """

    deliver: RouteDeliver
    note: str | None = None
    """A fact of the route, shown to the sender after a successful delivery."""


__all__ = [
    "SEND_TIMEOUT_SECONDS",
    "TUI_MOVED_MESSAGE",
    "Deliver",
    "DeliveryError",
    "DeliveryKind",
    "DeliveryRefused",
    "MessageRoute",
    "MessageSender",
    "RouteDeliver",
    "TransportDead",
]
