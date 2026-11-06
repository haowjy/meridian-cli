"""Subprocess-level regression coverage for bound harness environments."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.launch.env import build_harness_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import (
    resolve_project_runtime_root_for_write,
)
from tests.support.launch_paths import launch_paths

_PI_HELP = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions -e --extension PI_CODING_AGENT_SESSION_DIR"
)


def _write_recording_harness(path: Path, *, harness: HarnessId) -> None:
    main_protocol = (
        "for line in sys.stdin:\n"
        "    payload = json.loads(line)\n"
        "    if payload.get('type') == 'prompt':\n"
        "        print(json.dumps({'type': 'agent_start'}), flush=True)\n"
        "        print(json.dumps({'type': 'agent_end', 'messages': "
        "[{'role': 'assistant', 'stopReason': 'stop'}]}), flush=True)\n"
        "        print(json.dumps({'type': 'agent_settled', 'aborted': False}), flush=True)\n"
        "    elif payload.get('type') == 'abort':\n"
        "        raise SystemExit(0)\n"
        if harness is HarnessId.PI
        else "for _line in sys.stdin:\n    pass\n"
    )
    version_output = "pi 1.1.0" if harness is HarnessId.PI else "2.1.0"
    help_branch = (
        f"if '--help' in sys.argv[1:]:\n    print({_PI_HELP!r})\n    raise SystemExit(0)\n"
        if harness is HarnessId.PI
        else ""
    )
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "with open(os.environ['HARNESS_ENV_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
        "if '--version' in sys.argv[1:]:\n"
        f"    print({version_output!r})\n"
        "    raise SystemExit(0)\n"
        f"{help_branch}"
        f"{main_protocol}",
        encoding="utf-8",
    )
    path.chmod(0o755)


async def _wait_for_records(path: Path, count: int) -> list[dict[str, object]]:
    for _ in range(100):
        if path.exists():
            # The child appends JSONL while this observer polls. A visible file
            # can still end in an unfinished write; only consume committed lines.
            records = [
                json.loads(line)
                for line in path.read_text().splitlines(keepends=True)
                if line.endswith("\n")
            ]
            if len(records) >= count:
                return records
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} harness environment records at {path}")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_claude_probe_and_main_process_share_sanitized_bound_environment(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_recording_harness(bin_dir / "claude", harness=HarnessId.CLAUDE)
    env_log = tmp_path / "claude-env.jsonl"
    bound_home = tmp_path / "bound-home"
    child_env = build_harness_child_env(
        base_env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(bound_home),
            "HARNESS_ENV_LOG": str(env_log),
            "CLAUDECODE": "blocked-nesting-sentinel",
            "MERIDIAN_SECRET_PROBE": "should-not-reach-child",
        },
        adapter=ClaudeAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("claude-sonnet-4-6")),
    )
    connection = ClaudeConnection()
    spawn_id = SpawnId("p-env-boundary-claude")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness="claude",
        prompt="hello",
    )
    launch_paths(runtime_root, spawn_id).dir.mkdir(parents=True, exist_ok=True)
    await connection.start(
        ConnectionConfig(
            harness_id=HarnessId.CLAUDE,
            prompt="hello",
            control_root=tmp_path,
            launch=launch_paths(runtime_root, spawn_id),
            child_env=child_env,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.CLAUDE,
            prompt="hello",
        ),
    )
    try:
        records = await _wait_for_records(env_log, 2)
    finally:
        await connection.stop()

    assert any("--version" in record["argv"] for record in records)  # type: ignore[operator]
    assert any("--version" not in record["argv"] for record in records)  # type: ignore[operator]
    for record in records:
        env = record["env"]
        assert isinstance(env, dict)
        assert env["PATH"] == child_env["PATH"]
        assert env["HOME"] == str(bound_home)
        assert "CLAUDECODE" not in env
        assert "MERIDIAN_SECRET_PROBE" not in env


