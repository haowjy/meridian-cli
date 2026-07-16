# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner watchdog and finalization behavior."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import constants as launch_constants
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _build_request,
    _EndMonotonicFailsClock,
    _execute_with_context,
    _FakeControlSocketServer,
    _pi_extension_projection_fixture,
    _ReportThenHangConnection,
    streaming_runner_module,
)
from tests.support.fakes import FakeClock, FakeHeartbeat

_pi_extension_projection_fixture = _pi_extension_projection_fixture

def test_truncate_attempt_logs_removes_only_attempt_scoped_outputs(tmp_path: Path) -> None:
    log_dir = tmp_path / "spawns" / "r-truncate"
    log_dir.mkdir(parents=True, exist_ok=True)
    attempt_files = (
        launch_constants.HISTORY_FILENAME,
        launch_constants.STDERR_FILENAME,
        launch_constants.TOKENS_FILENAME,
        launch_constants.REPORT_FILENAME,
    )
    for name in attempt_files:
        (log_dir / name).write_text("attempt data\n", encoding="utf-8")
    durable_path = log_dir / "durable.json"
    durable_path.write_text("durable data\n", encoding="utf-8")

    streaming_runner_module._truncate_attempt_logs(log_dir)

    for name in attempt_files:
        assert not (log_dir / name).exists()
    assert durable_path.exists()


def test_retry_blocked_after_pi_child_started_detects_disk_child_state(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / "spawns" / "p2" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"id": "p2", "parent_id": "p1", "status": "running"}),
        encoding="utf-8",
    )

    assert streaming_runner_module._retry_blocked_after_pi_child_started(
        harness_id=HarnessId.PI,
        runtime_root=runtime_root,
        current_spawn_id=SpawnId("p1"),
    )


@pytest.mark.parametrize("entry_name", [".staging", ".p2", "spawn-stage"])
def test_retry_scan_ignores_non_spawn_row_entries(
    tmp_path: Path,
    entry_name: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / "spawns" / entry_name / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"id": entry_name, "parent_id": "p1", "status": "running"}),
        encoding="utf-8",
    )

    assert not streaming_runner_module._retry_blocked_after_pi_child_started(
        harness_id=HarnessId.PI,
        runtime_root=runtime_root,
        current_spawn_id=SpawnId("p1"),
    )

@pytest.mark.asyncio
async def test_execute_with_streaming_succeeds_after_report_watchdog_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ReportThenHangConnection,
    )
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_GRACE_SECONDS", 0.001)
    importlib.reload(streaming_runner_module)

    run = Spawn(
        spawn_id=SpawnId("r-watchdog"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-watchdog",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_request(),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=fake_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert fake_heartbeat.touches
    report = (runtime_root / "spawns" / str(run.spawn_id) / "report.md").read_text(encoding="utf-8")
    assert "Watchdog fallback completed." in report



@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_when_duration_clock_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    failing_clock = _EndMonotonicFailsClock(start=2_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(failing_clock)

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _ReportThenHangConnection,
    )
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_POLL_SECONDS", 0.001)
    monkeypatch.setattr(launch_constants, "REPORT_WATCHDOG_GRACE_SECONDS", 0.001)
    importlib.reload(streaming_runner_module)

    run = Spawn(
        spawn_id=SpawnId("r-duration-guard"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-duration-guard",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_request(),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=artifacts,
            registry=registry,
            clock=failing_clock,
            heartbeat_touch=fake_heartbeat.touch,
            heartbeat_interval_secs=0.001,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.exit_code == 0
    assert row.error is None
    assert row.duration_secs == 0.0
    assert fake_heartbeat.touches
