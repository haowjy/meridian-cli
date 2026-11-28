# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner watchdog and finalization behavior."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import constants as launch_constants
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn.model import FOREGROUND_LAUNCH_MODE
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
from tests.support.fakes import FakeClock
from tests.support.launch_paths import launch_paths

_pi_extension_projection_fixture = _pi_extension_projection_fixture


@dataclass
class _LifecycleRecorder:
    calls: list[str] = field(default_factory=list)

    def record_launch_facts(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("record_launch_facts")

    def record_exited(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("record_exited")


@dataclass
class _WatchdogManager:
    stop_calls: list[dict[str, object]] = field(default_factory=list)

    async def stop_spawn(self, spawn_id: SpawnId, **kwargs: object) -> None:
        self.stop_calls.append({"spawn_id": spawn_id, **kwargs})


@pytest.mark.asyncio
@pytest.mark.parametrize("report_exists", [False, True], ids=["before-report", "during-grace"])
async def test_report_watchdog_wakes_immediately_on_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    report_exists: bool,
) -> None:
    report_path = tmp_path / "report.md"
    if report_exists:
        report_path.touch()
    completion_event = asyncio.Event()
    manager = _WatchdogManager()
    spawn_id = SpawnId("report-completion")
    monkeypatch.setattr(streaming_runner_module, "REPORT_WATCHDOG_POLL_SECONDS", 60.0)

    task = asyncio.create_task(
        streaming_runner_module._report_watchdog(
            report_path=report_path,
            completion_event=completion_event,
            manager=manager,
            spawn_id=spawn_id,
        )
    )
    await asyncio.sleep(0)
    completion_event.set()

    assert await asyncio.wait_for(task, timeout=2.0) is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_streaming_attempt_bounds_backend_startup_with_no_events(
    tmp_path: Path,
) -> None:
    start_cancelled = asyncio.Event()

    class HangingStartupManager:
        async def join_teardown(self, spawn_id):
            pass

        async def stop_spawn(self, spawn_id, **kwargs):
            pass

        async def start_spawn(
            self,
            _config: ConnectionConfig,
            _spec: ResolvedLaunchSpec,
            *,
            event_hook=None,
        ) -> object:
            try:
                await asyncio.Event().wait()
            finally:
                start_cancelled.set()
            raise AssertionError("unreachable")

    run = Spawn(
        spawn_id=SpawnId("startup-hang"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="running",
    )
    attempt = await asyncio.wait_for(
        streaming_runner_module._run_streaming_attempt(
            run=run,
            launch=launch_paths(tmp_path, run.spawn_id),
            launch_mode=FOREGROUND_LAUNCH_MODE,
            manager=HangingStartupManager(),  # type: ignore[arg-type]
            config=ConnectionConfig(
                launch=launch_paths(tmp_path, run.spawn_id),
                harness_id=HarnessId.CODEX,
                prompt=run.prompt,
                control_root=tmp_path,
                child_env={},
            ),
            run_spec=ResolvedLaunchSpec(
                model=str(run.model),
                harness=HarnessId.CODEX,
            ),
            budget_tracker=None,
            signal_event=asyncio.Event(),
            received_signal=[None],
            timeout_seconds=None,
            startup_timeout_seconds=0.01,
            event_observer=None,
            stream_stdout_to_terminal=False,
            lifecycle_service=_LifecycleRecorder(),  # type: ignore[arg-type]
        ),
        timeout=3.0,
    )

    assert attempt.start_error == "startup phase timeout after 0.010s"
    assert start_cancelled.is_set()


@pytest.mark.asyncio
async def test_execute_with_streaming_succeeds_after_report_watchdog_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)

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
            registry=registry,
            clock=fake_clock,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.terminal.exit_code == 0
    assert row.terminal.error is None
    report = launch_paths(runtime_root, str(run.spawn_id)).report.read_text(encoding="utf-8")
    assert "Watchdog fallback completed." in report


@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_when_duration_clock_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    registry = HarnessRegistry.with_defaults()
    failing_clock = _EndMonotonicFailsClock(start=2_000.0)

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
            registry=registry,
            clock=failing_clock,
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.terminal.exit_code == 0
    assert row.terminal.error is None
    assert row.terminal.duration_secs == 0.0
