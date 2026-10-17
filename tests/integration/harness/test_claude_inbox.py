"""Claude primary inbox transport integration coverage."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.bundle import PrimaryInboxError
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.harness.claude_inbox import post_primary_message


def _publish_inbox(runtime_root: Path, spawn_id: SpawnId, pid: int, inbox: Path) -> None:
    journal = claude_boundary_journal_path(runtime_root, spawn_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "v": 2,
                "launch_nonce": "test",
                "pid": pid,
                "source": "startup",
                "session_id": "session",
                "transcript_path": str(runtime_root / "session.jsonl"),
                "messaging_socket": str(inbox),
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX inbox socket")
def test_claude_primary_inbox_posts_wire_message_and_reports_gone_socket(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p1")
    worker_pid = os.getpid()
    inbox = tmp_path / "inbox.sock"
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(inbox))
    server.listen()

    def receive() -> None:
        with server.accept()[0] as connection:
            chunks: list[bytes] = []
            while chunk := connection.recv(4096):
                chunks.append(chunk)
            received.append(b"".join(chunks))

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    try:
        _publish_inbox(tmp_path, spawn_id, worker_pid, inbox)
        receipt = post_primary_message(
            runtime_root=tmp_path,
            spawn_id=spawn_id,
            worker_pid=worker_pid,
            message="Reply exactly ACK",
        )
    finally:
        server.close()
        receiver.join(timeout=2)

    assert not receiver.is_alive()
    assert received == [
        b'{"type":"user","message":{"role":"user","content":"Reply exactly ACK"}}\n'
    ]
    assert receipt.confirmation == "Message posted to primary p1's Claude inbox"

    missing = tmp_path / "missing.sock"
    _publish_inbox(tmp_path, spawn_id, worker_pid, missing)
    with pytest.raises(PrimaryInboxError, match="Claude primary p1's inbox is gone"):
        post_primary_message(
            runtime_root=tmp_path,
            spawn_id=spawn_id,
            worker_pid=worker_pid,
            message="unreachable",
        )
