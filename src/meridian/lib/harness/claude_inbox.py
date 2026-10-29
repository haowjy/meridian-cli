"""Post messages to live Claude primary inbox sockets."""

from __future__ import annotations

import errno
import json
import socket
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.bundle import PrimaryInboxError
from meridian.lib.harness.claude_boundary import (
    claude_boundary_journal_path,
    latest_messaging_socket,
)
from meridian.lib.state.liveness import is_process_alive_born_by
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.spawn.model import SpawnRecord

_SOCKET_TIMEOUT_SECONDS = 3.0


def _gone(spawn_id: SpawnId) -> PrimaryInboxError:
    return PrimaryInboxError(f"Claude primary {spawn_id}'s inbox is gone")


def _process_exited(spawn_id: SpawnId) -> PrimaryInboxError:
    return PrimaryInboxError(
        f"Claude primary {spawn_id}'s inbox is gone because the Claude process exited"
    )


def post_primary_message(
    *,
    runtime_root: Path,
    record: SpawnRecord,
    message: str,
) -> str:
    """Post one user message to the inbox published by a live Claude primary."""

    spawn_id = SpawnId(record.id)
    worker_pid = record.worker_pid
    if worker_pid is None or worker_pid <= 0:
        raise PrimaryInboxError(f"Claude primary {spawn_id}'s worker PID is unknown")

    journal = claude_boundary_journal_path(LaunchPaths.for_row(runtime_root, record))
    published_inbox = latest_messaging_socket(journal, pid=worker_pid)
    if published_inbox is None:
        raise PrimaryInboxError(
            f"Claude primary {spawn_id} has not published an inbox socket "
            "(needs Claude Code >= 2.1.224; bare mode has none)"
        )
    inbox, recorded_at = published_inbox
    if not is_process_alive_born_by(worker_pid, recorded_at):
        raise _process_exited(spawn_id)

    wire_message = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": message},
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.settimeout(_SOCKET_TIMEOUT_SECONDS)
            client.connect(str(inbox))
            client.sendall(wire_message)
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

        try:
            client.shutdown(socket.SHUT_WR)
            while client.recv(4096):
                pass
        except OSError:
            pass

    return (
        f"Message posted to primary {spawn_id}'s Claude inbox\n"
        "Claude may hold it for approval when that session bypasses permission prompts; "
        'set crossSessionInbound to "accept" in Claude settings to skip the prompt.'
    )
