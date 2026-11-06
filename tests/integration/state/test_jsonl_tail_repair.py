from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.state.jsonl import (
    append_durable_jsonl_line,
    repair_jsonl_tail,
)
from meridian.lib.state.spawn_store import start_spawn
from tests.conftest import posix_only
from tests.support.launch_paths import launch_paths

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_permission_journal_repairs_torn_tail_before_append(tmp_path: Path) -> None:
    launch = launch_paths(tmp_path, "s-1")
    spawn_dir = launch.dir
    spawn_dir.mkdir(parents=True)
    journal_path = launch.permission_requests
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

    broker = PermissionBroker(launch=launch)
    assert [transition.seq for transition in broker.list_transitions()] == [0]

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

    reloaded = PermissionBroker(launch=launch)
    assert [transition.seq for transition in reloaded.list_transitions()] == [0, 1]


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
    actions_path = launch_paths(runtime_root, "s-1").control_actions
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

    coordinator = ControlActionCoordinator(launch=launch_paths(spawn_dir.parent.parent, "s-1"))
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

    reloaded = ControlActionCoordinator(launch=launch_paths(spawn_dir.parent.parent, "s-1"))
    assert reloaded._transition_seq > 1


@posix_only
def test_repair_jsonl_tail_preserves_existing_permissions(tmp_path: Path) -> None:
    path = tmp_path / "spawn" / "permission_requests.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"seq":0,"request_id":"req-1","status":"pending"}')
    os.chmod(path, 0o640)

    repair_jsonl_tail(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_bytes().endswith(b"\n")


def test_append_durable_jsonl_line_rejects_failed_repair_without_losing_next_event(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    torn = b'{"id":1,"kind":"start"}\n{"id":2,"kind":"to'
    path.write_bytes(torn)
    new_line = '{"id":3,"kind":"new"}\n'

    import meridian.lib.platform.atomic as platform_atomic

    def _deny_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        raise PermissionError("tail repair denied")

    with monkeypatch.context() as fault:
        fault.setattr(platform_atomic.os, "replace", _deny_replace)
        with pytest.raises(PermissionError, match="tail repair denied"):
            append_durable_jsonl_line(path, new_line)

    assert path.read_bytes() == torn

    append_durable_jsonl_line(path, new_line)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"id": 1, "kind": "start"}, {"id": 3, "kind": "new"}]
