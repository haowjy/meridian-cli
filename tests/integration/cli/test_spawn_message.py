"""CLI routing coverage for primary message injection."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from meridian.cli import spawn_message
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.state import spawn_store


@pytest.mark.asyncio
async def test_non_claude_primary_without_route_fails_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test-model",
        agent="test-agent",
        harness="codex",
        kind="primary",
        prompt="primary",
        worker_pid=12345,
    )
    monkeypatch.setattr(
        spawn_message,
        "resolve_runtime_root_and_config",
        lambda _root: (tmp_path, object()),
    )
    monkeypatch.setattr(spawn_message, "resolve_runtime_root", lambda _root: tmp_path)

    with pytest.raises(SystemExit):
        await asyncio.wait_for(
            spawn_message.send_message("p1", "hello"),
            timeout=1,
        )

    assert capsys.readouterr().err == (
        "Error: codex primaries have no message route yet (see #498)\n"
    )


@pytest.mark.asyncio
async def test_claude_primary_posts_to_published_inbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test-model",
        agent="test-agent",
        harness="claude",
        kind="primary",
        prompt="primary",
        worker_pid=os.getpid(),
    )
    inbox = tmp_path / "inbox.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(inbox))
    server.listen()
    received: list[bytes] = []

    def receive() -> None:
        with server.accept()[0] as connection:
            chunks: list[bytes] = []
            while chunk := connection.recv(4096):
                chunks.append(chunk)
            received.append(b"".join(chunks))

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    journal = claude_boundary_journal_path(tmp_path, "p1")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "v": 2,
                "launch_nonce": "test",
                "pid": os.getpid(),
                "recorded_at": time.time(),
                "source": "startup",
                "session_id": "session",
                "transcript_path": str(tmp_path / "session.jsonl"),
                "messaging_socket": str(inbox),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        spawn_message,
        "resolve_runtime_root_and_config",
        lambda _root: (tmp_path, object()),
    )
    monkeypatch.setattr(spawn_message, "resolve_runtime_root", lambda _root: tmp_path)

    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p2",
        chat_id="c1",
        model="test-model",
        agent="test-agent",
        harness="claude",
        prompt="child",
    )

    try:
        await spawn_message.send_message(
            "p1", "Reply exactly ACK", sender_spawn_id="p2"
        )
    finally:
        server.close()
        receiver.join(timeout=2)

    assert not receiver.is_alive()
    framed = (
        '[Meridian] Message from spawn p2 (chat c1). To answer, run '
        '`meridian spawn message c1 --message "..."`.\n\nReply exactly ACK'
    )
    assert received == [
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": framed}},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    ]
    assert capsys.readouterr().out == (
        "Message posted to primary p1's Claude inbox\n"
        "Claude may hold it for approval when that session bypasses permission prompts; "
        'set crossSessionInbound to "accept" in Claude settings to skip the prompt.\n'
    )
