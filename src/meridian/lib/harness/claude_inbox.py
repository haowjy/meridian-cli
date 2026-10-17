"""Post messages to live Claude primary inbox sockets."""

from __future__ import annotations

import errno
import json
import socket
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.bundle import (
    PrimaryInboxError,
    PrimaryInboxReceipt,
)
from meridian.lib.harness.claude_boundary import (
    claude_boundary_journal_path,
    latest_messaging_socket,
)

_SOCKET_TIMEOUT_SECONDS = 3.0


def _gone(spawn_id: SpawnId) -> PrimaryInboxError:
    return PrimaryInboxError(f"Claude primary {spawn_id}'s inbox is gone")


def post_primary_message(
    *,
    runtime_root: Path,
    spawn_id: SpawnId,
    worker_pid: int | None,
    message: str,
) -> PrimaryInboxReceipt:
    """Post one user message to the inbox published by a live Claude primary."""

    if worker_pid is None or worker_pid <= 0:
        raise PrimaryInboxError(f"Claude primary {spawn_id}'s worker PID is unknown")

    journal = claude_boundary_journal_path(runtime_root, spawn_id)
    inbox = latest_messaging_socket(journal, pid=worker_pid)
    if inbox is None:
        raise PrimaryInboxError(
            f"Claude primary {spawn_id} has not published an inbox socket "
            "(needs Claude Code >= 2.1.224; bare mode has none)"
        )

    wire_message = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": message},
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_SOCKET_TIMEOUT_SECONDS)
            client.connect(str(inbox))
            client.sendall(wire_message)
            client.shutdown(socket.SHUT_WR)
            while client.recv(4096):
                pass
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            raise _gone(spawn_id) from exc
        if isinstance(exc, TimeoutError):
            raise PrimaryInboxError(
                f"Claude primary {spawn_id}'s inbox timed out while posting the message"
            ) from exc
        detail = exc.strerror or str(exc)
        raise PrimaryInboxError(
            f"Could not post to Claude primary {spawn_id}'s inbox: {detail}"
        ) from exc

    return PrimaryInboxReceipt(
        confirmation=f"Message posted to primary {spawn_id}'s Claude inbox",
        advisory=(
            "Claude may hold it for approval when that session bypasses permission prompts."
        ),
    )
