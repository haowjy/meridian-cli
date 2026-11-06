"""Post messages to the inbox socket a live Claude primary publishes."""

from __future__ import annotations

import asyncio
import errno
import json
import socket
from pathlib import Path

from meridian.lib.core.delivery import (
    DeliveryKind,
    DeliveryRefused,
    MessageRoute,
    TransportDead,
)
from meridian.lib.harness.claude_boundary import (
    claude_boundary_journal_path,
    latest_messaging_socket,
)
from meridian.lib.state.paths import LaunchPaths

_SOCKET_TIMEOUT_SECONDS = 3.0
_ROUTE_NOTE = (
    "Claude may hold it for approval when that session bypasses permission prompts; "
    'set crossSessionInbound to "accept" in Claude settings to skip the prompt.'
)


def _post(inbox: Path, text: str) -> None:
    wire_message = (
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_SOCKET_TIMEOUT_SECONDS)
        try:
            client.connect(str(inbox))
        except TimeoutError as exc:
            raise DeliveryRefused("Claude's inbox timed out accepting a connection") from exc
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                raise TransportDead("Claude's inbox is gone") from exc
            raise DeliveryRefused(f"Could not reach Claude's inbox: {exc.strerror or exc}") from exc
        client.sendall(wire_message)

        try:
            client.shutdown(socket.SHUT_WR)
            while client.recv(4096):
                pass
        except OSError:
            pass


def claude_primary_message_route(launch: LaunchPaths, pid: int) -> MessageRoute | None:
    """The route to Claude process *pid*, or ``None`` until it has published an inbox.

    Each send looks the inbox up again: the journal is the authority for which socket
    is current.
    """

    journal = claude_boundary_journal_path(launch)
    if latest_messaging_socket(journal, pid=pid) is None:
        return None

    async def deliver(text: str) -> DeliveryKind:
        inbox = latest_messaging_socket(journal, pid=pid)
        if inbox is None:
            raise TransportDead("Claude's inbox is gone")
        await asyncio.to_thread(_post, inbox, text)
        return DeliveryKind.POSTED

    return MessageRoute(deliver, note=_ROUTE_NOTE)
