"""Pi RPC injection acknowledgement and busy-turn queueing integration tests."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from meridian.cli import spawn_inject as spawn_inject_module
from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent, StopResult
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import (
    build_runtime_from_root_and_config,
    resolve_runtime_authority_for_write,
)
from meridian.lib.ops.spawn import execute as spawn_execute_module
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.streaming.spawn_manager import SpawnManager

_PI_BUSY_REJECTION = (
    "Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') "
    "to queue the message."
)
_PI_HELP_SURFACE = (
    "--mode rpc --model --append-system-prompt --session --fork "
    "--session-dir --no-extensions --no-skills "
    "--no-context-files --no-prompt-templates -e --extension "
    "PI_CODING_AGENT_SESSION_DIR"
)


class _NoopControlServer:
    endpoint = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _configure_pi_runtime(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    error: str | None,
) -> Path:
    source_root = root / "dist" / "extensions"
    for extension_name in ("managed-bash", "meridian-spawn-watch"):
        extension_dir = source_root / extension_name
        extension_dir.mkdir(parents=True, exist_ok=True)
        (extension_dir / "index.js").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(root / "agent" / "extensions"))

    inbound_log = root / "pi-inbound.jsonl"
    bin_dir = root / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"HELP = {_PI_HELP_SURFACE!r}\n"
        f"REJECTION = {error!r}\n"
        "if '--version' in sys.argv[1:]: print('pi 0.80.7'); raise SystemExit\n"
        "if '--help' in sys.argv[1:]: print(HELP); raise SystemExit\n"
        "prompt_count = 0\n"
        "for line in sys.stdin:\n"
        "    command = json.loads(line)\n"
        "    with open(os.environ['PI_RPC_INBOUND_LOG'], 'a', encoding='utf-8') as log:\n"
        "        log.write(json.dumps(command) + '\\n')\n"
        "    if command['type'] == 'prompt':\n"
        "        prompt_count += 1\n"
        "        accepted = prompt_count == 1 or REJECTION is None\n"
        "        response = {'type': 'response', 'command': 'prompt', 'success': accepted}\n"
        "        if 'id' in command: response['id'] = command['id']\n"
        "        if not accepted: response['error'] = REJECTION\n"
        "        print(json.dumps(response), flush=True)\n"
        "        if accepted:\n"
        "            print(json.dumps({'type': 'agent_start'}), flush=True)\n"
        "        if prompt_count > 1:\n"
        "            print(json.dumps({'type': 'agent_end', 'messages': "
        "[{'role': 'assistant', 'stopReason': 'stop'}]}), flush=True)\n"
        "    elif command['type'] == 'abort':\n"
        "        raise SystemExit\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_RPC_INBOUND_LOG", str(inbound_log))
    return inbound_log


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX control socket")
async def test_immediate_background_inject_waits_for_real_control_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="meridian-inject-", dir="/tmp"))
    received_messages: list[str] = []

    class _DelayedBackgroundConnection:
        harness_id = HarnessId.CODEX
        primary_event_scope = None
        resident_backend = None
        subprocess_pid = None
        session_id = "delayed-background"

        def __init__(self) -> None:
            self._events: asyncio.Queue[RawHarnessEvent | None] = asyncio.Queue()

        def observe_event_semantics(self, semantics: object) -> None:
            _ = semantics

        async def events(self):  # type: ignore[no-untyped-def]
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        async def send_user_message(self, message: str) -> None:
            received_messages.append(message)
            await self._events.put(
                RawHarnessEvent(
                    event_type="turn/completed",
                    harness_id="codex",
                    payload={"status": "succeeded", "exit_code": 0},
                )
            )

        async def send_cancel(self) -> None:
            return None

        async def stop(self, **_kwargs: object) -> StopResult:
            await self._events.put(None)
            return StopResult()

    connection = _DelayedBackgroundConnection()

    async def _delayed_background_start(
        _config: ConnectionConfig,
        _spec: ResolvedLaunchSpec,
    ) -> _DelayedBackgroundConnection:
        await asyncio.sleep(3.5)
        return connection

    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", str(runtime_root / "home"))
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None
    runtime = build_runtime_from_root_and_config(
        project_root,
        load_config(project_root, authority=authority),
        authority=authority,
    )

    class _FakePopen:
        pid = 42424

    monkeypatch.setattr(spawn_execute_module.subprocess, "Popen", lambda *_a, **_kw: _FakePopen())
    launch = spawn_execute_module.execute_spawn_background(
        payload=SpawnCreateInput(prompt="FIRST", background=True),
        request=SpawnRequest(
            prompt="FIRST",
            model="pi-test-model",
            harness=HarnessId.CODEX.value,
        ),
        runtime=runtime,
    )
    assert launch.status == "running"
    assert launch.spawn_id is not None
    spawn_id = SpawnId(launch.spawn_id)
    control_root = authority.runtime_root
    manager = SpawnManager(
        runtime_root=control_root,
        project_root=project_root,
        start_connection=_delayed_background_start,
    )
    monkeypatch.setattr(
        spawn_inject_module,
        "resolve_runtime_root_and_config",
        lambda _root: (project_root, object()),
    )
    monkeypatch.setattr(
        spawn_inject_module,
        "resolve_runtime_root",
        lambda _root: control_root,
    )

    launch_task = asyncio.create_task(
        manager.start_spawn(
            ConnectionConfig(
                spawn_id=spawn_id,
                harness_id=HarnessId.CODEX,
                prompt="FIRST",
                control_root=project_root,
                env_overrides={},
            ),
            ResolvedLaunchSpec(
                harness=HarnessId.CODEX,
                prompt="FIRST",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
        )
    )

    try:
        await asyncio.wait_for(
            spawn_inject_module.inject_message(str(spawn_id), "SECOND"),
            timeout=10.0,
        )
        await asyncio.wait_for(launch_task, timeout=5.0)
        outcome = await asyncio.wait_for(
            manager.wait_for_completion(spawn_id),
            timeout=5.0,
        )

        assert outcome is not None
        assert outcome.status == "succeeded"
        assert "Message delivered" in capsys.readouterr().out
        assert received_messages == ["SECOND"]
    finally:
        if not launch_task.done():
            launch_task.cancel()
        cleanup_task = manager._cleanup_tasks.get(spawn_id)
        if cleanup_task is not None:
            await asyncio.wait_for(cleanup_task, timeout=5.0)
        await manager.shutdown()
        shutil.rmtree(runtime_root, ignore_errors=True)


async def _start_pi_connection(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> PiRpcConnection:
    connection = PiRpcConnection()
    await connection.start(config, spec)
    return connection


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_busy_turn_inject_queues_follow_up_and_completes_both_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound_log = _configure_pi_runtime(monkeypatch, tmp_path, None)
    spawn_id = SpawnId("p-pi-busy-inject")
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness="pi",
        prompt="FIRST",
    )
    resolve_spawn_log_dir(tmp_path, spawn_id).mkdir(parents=True)
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_pi_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="FIRST",
            control_root=tmp_path,
            runtime_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="FIRST",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        inject_result = await manager.inject(spawn_id, "SECOND", source="test")
        outcome = await manager.wait_for_completion(spawn_id)

        assert inject_result.success is True
        assert outcome is not None
        assert outcome.status == "succeeded"

        commands = [
            json.loads(line)
            for line in inbound_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        prompts = [command for command in commands if command["type"] == "prompt"]
        assert [prompt["message"] for prompt in prompts] == ["FIRST", "SECOND"]
        assert all(prompt["streamingBehavior"] == "followUp" for prompt in prompts)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
@pytest.mark.parametrize("rejection", [_PI_BUSY_REJECTION, "Future Pi prompt rejection"])
async def test_pi_rejected_inject_is_reported_and_does_not_fail_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
) -> None:
    inbound_log = _configure_pi_runtime(monkeypatch, tmp_path, rejection)
    spawn_id = SpawnId("p-pi-rejected-inject")
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness="pi",
        prompt="FIRST",
    )
    resolve_spawn_log_dir(tmp_path, spawn_id).mkdir(parents=True)
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_pi_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="FIRST",
            control_root=tmp_path,
            runtime_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="FIRST",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        inject_result = await manager.inject(spawn_id, "SECOND", source="test")
        outcome = await manager.wait_for_completion(spawn_id)

        assert inject_result.success is False
        assert inject_result.error == rejection
        assert outcome is not None
        assert outcome.status == "succeeded"

        commands = [
            json.loads(line)
            for line in inbound_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        prompts = [command for command in commands if command["type"] == "prompt"]
        assert [prompt["message"] for prompt in prompts] == ["FIRST", "SECOND"]
        assert all(prompt["streamingBehavior"] == "followUp" for prompt in prompts)
    finally:
        await manager.shutdown()
