"""Managed stdio process ownership tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import psutil
import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import managed_stdio
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn_store import start_spawn


@pytest.mark.asyncio
async def test_launch_managed_stdio_reaps_child_when_registration_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("stdio-registration-failure")
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test-model",
        agent="tester",
        harness="pi",
        prompt="test",
        status="running",
    )
    launched: list[asyncio.subprocess.Process] = []

    async def fail_registration(**kwargs: object) -> object:
        launched.append(kwargs["process"])  # type: ignore[arg-type]
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(
        managed_stdio,
        "register_spawn_owned_process",
        fail_registration,
    )

    try:
        with pytest.raises(RuntimeError, match="injected registration failure"):
            await managed_stdio.launch_managed_stdio(
                config=ConnectionConfig(
                    spawn_id=spawn_id,
                    harness_id=HarnessId.PI,
                    prompt="test",
                    control_root=tmp_path,
                    child_env={},
                    runtime_root=runtime_root,
                ),
                harness_id=HarnessId.PI,
                command=(sys.executable, "-c", "import time; time.sleep(60)"),
                env=os.environ.copy(),
                cwd=str(tmp_path),
                stdin=asyncio.subprocess.PIPE,
                stdout_limit=64 * 1024,
                kill_grace_seconds=0.1,
                terminate_reason="test_cleanup",
            )

        assert len(launched) == 1
        await asyncio.wait_for(launched[0].wait(), timeout=1.0)
        stderr_log = runtime_root / "spawns" / str(spawn_id) / "stderr.log"
        assert stderr_log.resolve() not in {
            Path(open_file.path).resolve()
            for open_file in psutil.Process().open_files()
        }
    finally:
        for process in launched:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
