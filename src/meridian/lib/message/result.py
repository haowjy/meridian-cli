"""What ``meridian message`` returns, whichever route carried the message.

One result type for the control socket, the people program and (later) a saved machine,
and one printer, so a route can't print or exit on its own and every failure has a kind.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Final

from meridian.lib.core.delivery import DeliveryKind

# The closed list: only ``maybe_delivered`` means the message may have arrived. Another
# kind asserts that nothing was sent, so a script may send again.
ERROR_KINDS: Final = frozenset(
    {
        "usage",
        "not_found",
        "ambiguous",
        "not_running",
        "no_route",
        "too_large",
        "sandboxed",
        "refused",
        "version",
        "unreachable",
        "auth",
        "host_key",
        "maybe_delivered",
        "failed",
    }
)


@dataclass(frozen=True)
class Receiver:
    """What is known of the receiver, filled in as far as the address resolved."""

    address: str | None = None
    spawn_id: str | None = None
    chat_id: str | None = None
    machine: str | None = None


@dataclass(frozen=True)
class Delivered:
    delivery: DeliveryKind
    to: str
    """The receiver as the text output names it: ``spawn p1`` or ``@alice``."""
    note: str | None = None
    receiver: Receiver = field(default_factory=Receiver)


@dataclass(frozen=True)
class Failed:
    kind: str
    text: str
    receiver: Receiver = field(default_factory=Receiver)


type MessageResult = Delivered | Failed


def delivery_kind(value: object) -> DeliveryKind:
    """The delivery a route reported; one it doesn't name is the weakest claim, posted."""

    if value == DeliveryKind.ACKNOWLEDGED:
        return DeliveryKind.ACKNOWLEDGED
    if value == DeliveryKind.QUEUED:
        return DeliveryKind.QUEUED
    return DeliveryKind.POSTED


def error_kind(value: object, *, unknown: str) -> str:
    """A kind a route reported; anything not on the list becomes ``unknown``."""

    return value if isinstance(value, str) and value in ERROR_KINDS else unknown


def result_json(result: MessageResult) -> dict[str, object]:
    """The one object ``--json`` prints for every outcome."""

    receiver = result.receiver
    payload: dict[str, object] = {"ok": isinstance(result, Delivered)}
    for key, value in (
        ("address", receiver.address),
        ("machine", receiver.machine),
        ("spawn_id", receiver.spawn_id),
        ("chat_id", receiver.chat_id),
    ):
        if value is not None:
            payload[key] = value
    if isinstance(result, Delivered):
        payload["delivery"] = result.delivery.value
        if result.note is not None:
            payload["note"] = result.note
    else:
        payload["error"] = {"kind": result.kind, "text": result.text}
    return payload


def print_startup_error(text: str) -> None:
    """Print an error raised before the handler ran (a bad option, a missing value)."""

    payload = {"ok": False, "error": {"kind": "usage", "text": text}}
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def print_result(result: MessageResult, *, json_mode: bool) -> int:
    """Print the result as text or JSON and return the exit status."""

    if json_mode:
        print(json.dumps(result_json(result), separators=(",", ":"), sort_keys=True))
    elif isinstance(result, Delivered):
        match result.delivery:
            case DeliveryKind.ACKNOWLEDGED:
                print(f"Message delivered to {result.to}")
            case DeliveryKind.QUEUED:
                print(f"Message queued for {result.to}")
            case DeliveryKind.POSTED:
                print(f"Message posted to {result.to} (no receipt)")
        if result.note is not None:
            print(result.note)
    else:
        print(f"Error: {result.text}", file=sys.stderr)
    return 0 if isinstance(result, Delivered) else 1


__all__ = [
    "ERROR_KINDS",
    "Delivered",
    "Failed",
    "MessageResult",
    "Receiver",
    "delivery_kind",
    "error_kind",
    "print_result",
    "print_startup_error",
    "result_json",
]
