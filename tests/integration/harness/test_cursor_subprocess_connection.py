"""Cursor subprocess session attribution and create-chat fallback coverage."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import cursor_subprocess
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.cursor_subprocess import CursorSubprocessConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.executables import prepend_fake_executables

_MINTED_ID = "550e8400-e29b-41d4-a716-446655440000"
_RESUMED_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _install_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    create_chat_exit: int = 0,
    create_chat_hangs: bool = False,
) -> Path:
    prepend_fake_executables(monkeypatch, tmp_path, "cursor")
    command_log = tmp_path / "cursor-commands.jsonl"
    cursor = tmp_path / "fake-bin" / "cursor"
    cursor.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CURSOR_COMMAND_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if args == ['agent', 'create-chat']:\n"
        "    if os.environ.get('CURSOR_CREATE_CHAT_PID'):\n"
        "        with open(os.environ['CURSOR_CREATE_CHAT_PID'], 'w') as pid_file:\n"
        "            pid_file.write(str(os.getpid()))\n"
        f"    if {create_chat_hangs!r}:\n"
        "        time.sleep(60)\n"
        f"    print({_MINTED_ID!r}, flush=True)\n"
        f"    raise SystemExit({create_chat_exit})\n"
        "print(json.dumps({'type': 'system', 'sessionId': 'fake-agent'}), flush=True)\n"
        "time.sleep(0.05)\n",
        encoding="utf-8",
    )
    cursor.chmod(0o755)
    monkeypatch.setenv("CURSOR_COMMAND_LOG", str(command_log))
    return command_log


def _start_record(runtime_root: Path, spawn_id: SpawnId) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness="cursor",
        prompt="hello",
    )
    resolve_spawn_log_dir(runtime_root, spawn_id, runtime_root=runtime_root).mkdir(
        parents=True, exist_ok=True
    )


async def _start(
    tmp_path: Path,
    spawn_id: SpawnId,
    *,
    continue_session_id: str | None = None,
) -> CursorSubprocessConnection:
    _start_record(tmp_path, spawn_id)
    child_env = {
        "PATH": os.environ["PATH"],
        "CURSOR_COMMAND_LOG": os.environ["CURSOR_COMMAND_LOG"],
    }
    if create_chat_pid := os.environ.get("CURSOR_CREATE_CHAT_PID"):
        child_env["CURSOR_CREATE_CHAT_PID"] = create_chat_pid

    def record_session(session_id: str) -> None:
        spawn_store.update_spawn(
            tmp_path,
            spawn_id,
            harness_session_id=session_id,
        )

    connection = CursorSubprocessConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CURSOR,
            prompt="hello",
            control_root=tmp_path,
            runtime_root=tmp_path,
            child_env=child_env,
            session_id_observer=record_session,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.CURSOR,
            prompt="hello",
            continue_session_id=continue_session_id,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    return connection


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_cursor_create_chat_and_resume_reuse_session_with_store_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_log = _install_cursor(monkeypatch, tmp_path)

    fresh_id = SpawnId("p-cursor-fresh")
    fresh = await _start(tmp_path, fresh_id)
    assert fresh.session_id == _MINTED_ID
    assert str(spawn_store.get_spawn(tmp_path, fresh_id).harness_session_id) == _MINTED_ID
    await fresh.stop()

    resume_id = SpawnId("p-cursor-resume")
    resumed = await _start(tmp_path, resume_id, continue_session_id=_RESUMED_ID)
    assert resumed.session_id == _RESUMED_ID
    assert str(spawn_store.get_spawn(tmp_path, resume_id).harness_session_id) == _RESUMED_ID
    await resumed.stop()

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert commands[0] == ["agent", "create-chat"]
    agent_commands = [command for command in commands if command != ["agent", "create-chat"]]
    assert len(agent_commands) == 2
    assert agent_commands[0][agent_commands[0].index("--resume") + 1] == _MINTED_ID
    assert agent_commands[1][agent_commands[1].index("--resume") + 1] == _RESUMED_ID


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_cursor_nonzero_create_chat_falls_back_to_unresumed_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_log = _install_cursor(monkeypatch, tmp_path, create_chat_exit=7)

    connection = await _start(tmp_path, SpawnId("p-cursor-fallback"))
    assert connection.session_id is None
    assert spawn_store.get_spawn(
        tmp_path, SpawnId("p-cursor-fallback")
    ).harness_session_id is None
    await connection.stop()

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert commands[0] == ["agent", "create-chat"]
    assert "--resume" not in commands[1]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_cursor_hung_create_chat_is_killed_then_launches_unresumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_log = _install_cursor(monkeypatch, tmp_path, create_chat_hangs=True)
    create_chat_pid = tmp_path / "create-chat.pid"
    monkeypatch.setenv("CURSOR_CREATE_CHAT_PID", str(create_chat_pid))
    monkeypatch.setattr(cursor_subprocess, "_CREATE_CHAT_TIMEOUT_SECONDS", 0.05)

    connection = await asyncio.wait_for(
        _start(tmp_path, SpawnId("p-cursor-hung-create-chat")),
        timeout=1.0,
    )
    assert connection.session_id is None
    assert spawn_store.get_spawn(
        tmp_path, SpawnId("p-cursor-hung-create-chat")
    ).harness_session_id is None

    pid = int(create_chat_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    await connection.stop()
    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert commands[0] == ["agent", "create-chat"]
    assert "--resume" not in commands[1]
