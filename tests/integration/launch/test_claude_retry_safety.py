"""Composed Claude retry-safety regressions at the real subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import RetryPolicy, SpawnRequest
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _execute_with_context,
    _FakeControlSocketServer,
)


@pytest.mark.asyncio
async def test_explicit_claude_terminal_result_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed upstream terminal result must remain the one causal failure."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations.jsonl"
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
        "session_id=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\\n' \"$session_id\"\n"
        "printf '%s\\n' "
        "'{\"type\":\"result\",\"subtype\":\"error_max_turns\",\"is_error\":true,"
        "\"result\":\"subscription quota exhausted\"}'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-terminal-no-replay"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-terminal",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = SpawnRequest(
        model=str(run.model),
        harness=HarnessId.CLAUDE.value,
        prompt=run.prompt,
        retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 1
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "subscription quota exhausted"
    assert len(invocation_log.read_text(encoding="utf-8").splitlines()) == 1
    lifecycle_rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["attempt"] for row in lifecycle_rows if row["event"] == "attempt_started"] == [
        1
    ]
