"""Claude primary inbox transport integration coverage."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meridian.lib.harness.bundle import PrimaryInboxError
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.harness.claude_inbox import post_primary_message
from meridian.lib.state.spawn.model import SpawnRecord


def _primary_record(pid: int) -> SpawnRecord:
    return SpawnRecord(
        id="p1",
        harness="claude",
        kind="primary",
        status="running",
        worker_pid=pid,
    )


def _publish_inbox(
    runtime_root: Path,
    record: SpawnRecord,
    inbox: Path,
    *,
    recorded_at: float,
) -> None:
    journal = claude_boundary_journal_path(runtime_root, record.id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "v": 2,
                "launch_nonce": "test",
                "pid": record.worker_pid,
                "recorded_at": recorded_at,
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
    record = _primary_record(os.getpid())
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
        _publish_inbox(tmp_path, record, inbox, recorded_at=time.time())
        confirmation = post_primary_message(
            runtime_root=tmp_path,
            record=record,
            message="Reply exactly ACK",
        )
    finally:
        server.close()
        receiver.join(timeout=2)

    assert not receiver.is_alive()
    assert received == [
        b'{"type":"user","message":{"role":"user","content":"Reply exactly ACK"}}\n'
    ]
    assert confirmation == (
        "Message posted to primary p1's Claude inbox\n"
        "Claude may hold it for approval when that session bypasses permission prompts."
    )

    missing = tmp_path / "missing.sock"
    _publish_inbox(tmp_path, record, missing, recorded_at=time.time())
    with pytest.raises(PrimaryInboxError, match="Claude primary p1's inbox is gone"):
        post_primary_message(
            runtime_root=tmp_path,
            record=record,
            message="unreachable",
        )


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX process semantics")
def test_claude_primary_inbox_refuses_pid_born_after_socket_record(tmp_path: Path) -> None:
    recorded_at = time.time() - 10
    inbox = tmp_path / "unrelated.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(inbox))
    server.listen()
    server.settimeout(0.05)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        record = _primary_record(child.pid)
        _publish_inbox(
            tmp_path,
            record,
            inbox,
            recorded_at=recorded_at,
        )

        with pytest.raises(PrimaryInboxError, match="Claude process exited"):
            post_primary_message(
                runtime_root=tmp_path,
                record=record,
                message="must not be posted",
            )
        assert child.poll() is None
        with pytest.raises(TimeoutError):
            server.accept()
    finally:
        server.close()
        child.terminate()
        child.wait(timeout=5)
