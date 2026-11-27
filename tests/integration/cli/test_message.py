"""CLI to a Claude primary: message, control socket, published inbox."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from meridian.cli import message as message_cli
from meridian.lib.core.delivery import (
    DeliveryKind,
    DeliveryRefused,
    MessageRoute,
    TransportDead,
)
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.harness.claude_inbox import claude_primary_message_route
from meridian.lib.launch.process.primary_control_socket import primary_control_socket
from meridian.lib.message.result import Delivered, Failed
from meridian.lib.ops import locate as locate_module
from meridian.lib.state import spawn_store
from meridian.lib.state.machine_ids import allocate
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.streaming.control_socket import (
    ControlSocketServer,
    control_socket_path,
    prepare_socket_directory,
)
from tests.support.launch_paths import launch_paths


@pytest.mark.asyncio
async def test_claude_primary_message_reaches_published_inbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    journal = claude_boundary_journal_path(launch_paths(tmp_path, "p1"))
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
        message_cli,
        "resolve_runtime_root_and_config",
        lambda _root: (tmp_path, object()),
    )
    monkeypatch.setattr(message_cli, "resolve_runtime_root", lambda _root: tmp_path)
    monkeypatch.setattr(locate_module, "resolve_runtime_root_for_read", lambda _root: tmp_path)

    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p2",
        chat_id="c1",
        owner_chat_id="c9",
        model="test-model",
        agent="test-agent",
        harness="claude",
        prompt="child",
    )

    # The child sender is reachable only while its control socket exists.
    child_socket = control_socket_path(tmp_path, "p2")
    prepare_socket_directory(child_socket.parent)
    child_socket.touch()

    try:
        with primary_control_socket(
            launch_paths(tmp_path, "p1"), claude_primary_message_route
        ) as serve:
            serve(os.getpid())
            result = await message_cli.send_message("p1", "Reply exactly ACK", sender_spawn_id="p2")
    finally:
        server.close()
        receiver.join(timeout=2)

    assert not receiver.is_alive()
    framed = (
        "[Meridian] Message from spawn p2 (chat c1). To answer, run "
        '`meridian message c1 "..."`.\n\nReply exactly ACK'
    )
    assert received == [
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": framed}},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    ]
    assert not control_socket_path(tmp_path, "p1").exists()
    assert isinstance(result, Delivered)
    assert result.delivery == DeliveryKind.POSTED
    assert result.receiver.spawn_id == "p1"
    assert result.note == (
        "Claude may hold it for approval when that session bypasses permission prompts; "
        'set crossSessionInbound to "accept" in Claude settings to skip the prompt.'
    )


@pytest.fixture
def running_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A running spawn ``p1`` in ``tmp_path`` whose control socket the test serves."""

    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test-model",
        agent="test-agent",
        harness="claude",
        prompt="x",
        worker_pid=os.getpid(),
    )
    monkeypatch.setattr(
        message_cli, "resolve_runtime_root_and_config", lambda _root: (tmp_path, object())
    )
    monkeypatch.setattr(message_cli, "resolve_runtime_root", lambda _root: tmp_path)
    monkeypatch.setattr(locate_module, "resolve_runtime_root_for_read", lambda _root: tmp_path)
    path = control_socket_path(tmp_path, "p1")
    prepare_socket_directory(path.parent)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (DeliveryRefused("no"), "refused"),
        (TransportDead("gone"), "not_running"),
        (RuntimeError("after the write"), "maybe_delivered"),
    ],
)
async def test_server_failure_kind_reaches_the_sender(
    running_session: Path, failure: Exception, kind: str
) -> None:
    async def deliver(_text: str, _sender: object) -> DeliveryKind:
        raise failure

    server = ControlSocketServer(running_session, MessageRoute(deliver=deliver))
    await server.start()
    try:
        result = await message_cli.send_message("p1", "hello")
    finally:
        await server.stop()

    assert isinstance(result, Failed)
    assert (result.kind, result.text) == (kind, str(failure))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        b'{"ok":false,"error":"older build, no kind"}\n',
        b'{"ok":false,"error":"' + b"x" * 100_000 + b'"}\n',
        b"not json\n",
        b"",
    ],
    ids=["no-kind", "over-64KiB-line", "not-json", "no-reply"],
)
async def test_message_that_got_no_parsed_answer_may_have_arrived(
    running_session: Path, reply: bytes
) -> None:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(answer, path=str(running_session))
    try:
        result = await message_cli.send_message("p1", "hello")
    finally:
        server.close()

    assert isinstance(result, Failed)
    assert result.kind == "maybe_delivered"


def test_reply_address_names_dash_c_after_the_address_for_a_legacy_sender_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    claimed = allocate("c", tmp_path / "runtime-root")

    def reply(chat_id: str) -> str:
        sender = SpawnRecord.model_construct(id="p9", current_chat_id=chat_id, run_boundary=None)
        return message_cli.framed_message(
            "hi",
            sender_spawn_id="p9",
            sender=sender,
            can_reply=True,
            reply_project=Path("/work/repo"),
        )

    assert f'`meridian message {claimed} "..."`' in reply(claimed)
    assert '`meridian message c5 -C /work/repo "..."`' in reply("c5")
