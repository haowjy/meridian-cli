"""Pi RPC injection acknowledgement and busy-turn queueing integration tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
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
    monkeypatch: pytest.MonkeyPatch, root: Path, error: str | None
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
