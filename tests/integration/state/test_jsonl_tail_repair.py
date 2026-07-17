from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.state.atomic import (
    _jsonl_tail_needs_repair,
    append_durable_jsonl_line,
    repair_jsonl_tail,
)
from meridian.lib.state.spawn_store import start_spawn
from tests.conftest import posix_only


def test_permission_journal_repairs_torn_tail_before_append(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawn"
    spawn_dir.mkdir()
    journal_path = spawn_dir / "permission_requests.jsonl"
    complete_row = {
        "seq": 0,
        "request_id": "req-1",
        "request_type": "approval",
        "method": "tool",
        "payload": {"name": "bash"},
        "status": "pending",
        "resolution": None,
        "error": None,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    torn_tail = (
        b'{"seq":1,"request_id":"req-2","request_type":"approval",'
        b'"method":"tool","payload":{},"status":"pend'
    )
    journal_path.write_bytes(
        (json.dumps(complete_row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        + torn_tail
    )

    broker = PermissionBroker(spawn_dir=spawn_dir)
    assert broker._next_seq == 1

    next_row = {
        "seq": 1,
        "request_id": "req-2",
        "request_type": "approval",
        "method": "tool",
        "payload": {},
        "status": "pending",
        "resolution": None,
        "error": None,
        "timestamp": "2026-01-01T00:00:01Z",
    }
    append_durable_jsonl_line(
        journal_path,
        json.dumps(next_row, sort_keys=True, separators=(",", ":")) + "\n",
    )

    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["seq"] for row in rows] == [0, 1]
    journal_lines = [
        line for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert all(json.loads(line) for line in journal_lines)

    reloaded = PermissionBroker(spawn_dir=spawn_dir)
    assert reloaded._next_seq == 2


@pytest.mark.asyncio
async def test_control_journal_repairs_torn_tail_without_reusing_transition_seq(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_dir = runtime_root / "spawns" / "s-1"
    start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="codex",
        prompt="test",
        spawn_id="s-1",
    )
    actions_path = spawn_dir / "control_actions.jsonl"
    complete_row = {
        "seq": 0,
        "spawn_id": "s-1",
        "action_id": "ca-0",
        "action": "interrupt",
        "status": "requested",
        "submission_seq": 0,
        "attempt": 0,
        "interrupt_epoch": 0,
        "source": "test",
        "ts": 1.0,
        "payload": {},
    }
    actions_path.write_bytes(
        (json.dumps(complete_row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        + b'{"seq":1,"spawn_id":"s-1","action_id":"ca-1","action":"inject","status":"request'
    )

    coordinator = ControlActionCoordinator(spawn_id="s-1", spawn_dir=spawn_dir)
    assert coordinator._transition_seq == 1

    async def _noop_send() -> object:
        return None

    await coordinator.run_action(
        action=ControlActionType.INJECT,
        payload={"text": "hello"},
        source="test",
        send=_noop_send,
        fence_on_interrupt=False,
    )

    rows = [
        json.loads(line)
        for line in actions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seqs = [row["seq"] for row in rows]
    assert seqs[0] == 0
    assert 1 in seqs
    assert len(seqs) == len(set(seqs))

    reloaded = ControlActionCoordinator(spawn_id="s-1", spawn_dir=spawn_dir)
    assert reloaded._transition_seq > 1


def test_append_durable_jsonl_line_preserves_complete_row_missing_newline(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    complete = '{"id":1,"kind":"complete"}'
    path.write_bytes(complete.encode())

    append_durable_jsonl_line(path, '{"id":2,"kind":"new"}\n')

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"id": 1, "kind": "complete"}, {"id": 2, "kind": "new"}]


def test_jsonl_tail_needs_repair_is_false_for_clean_tail(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"id":1,"kind":"x"}\n')

    assert _jsonl_tail_needs_repair(path) is False


def test_jsonl_tail_needs_repair_is_false_for_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"

    assert _jsonl_tail_needs_repair(path) is False


def test_append_durable_jsonl_line_avoids_full_read_on_clean_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    row = b'{"id":1,"kind":"x"}\n'
    path.write_bytes(row * 100_000)

    read_calls = 0
    original_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        nonlocal read_calls
        if self == path:
            read_calls += 1
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    append_durable_jsonl_line(path, '{"id":2,"kind":"y"}\n')

    assert read_calls == 0


@posix_only
def test_repair_jsonl_tail_preserves_existing_permissions(tmp_path: Path) -> None:
    path = tmp_path / "spawn" / "permission_requests.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"seq":0,"request_id":"req-1","status":"pending"}')
    os.chmod(path, 0o640)

    repair_jsonl_tail(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_bytes().endswith(b"\n")


def test_repair_jsonl_tail_does_not_create_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "spawn" / "launch-boundary.jsonl"

    repair_jsonl_tail(path)

    assert not path.parent.exists()
