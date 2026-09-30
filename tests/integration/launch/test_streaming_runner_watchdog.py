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
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.harness.semantics import EventSemantics, NormalizedHarnessEvent
from meridian.lib.launch import constants as launch_constants
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn.model import FOREGROUND_LAUNCH_MODE
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from meridian.lib.streaming.spawn_session import DrainOutcome
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

@dataclass
class _LifecycleRecorder:
    calls: list[str] = field(default_factory=list)

    def mark_running(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("mark_running")

    def record_exited(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("record_exited")


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

        def get_connection(self, _spawn_id: SpawnId) -> None:
            return None

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
            runtime_root=tmp_path,
            launch_mode=FOREGROUND_LAUNCH_MODE,
            log_dir=tmp_path / "logs",
            manager=HangingStartupManager(),  # type: ignore[arg-type]
            config=ConnectionConfig(
                spawn_id=run.spawn_id,
                harness_id=HarnessId.CODEX,
                prompt=run.prompt,
                control_root=tmp_path,
                child_env={},
            ),
            run_spec=ResolvedLaunchSpec(
                model=str(run.model),
                harness=HarnessId.CODEX,
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
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
async def test_streaming_attempt_fresh_events_keep_slow_cursor_backend_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: asyncio.Queue[NormalizedHarnessEvent | None] = asyncio.Queue()
    completion = asyncio.Event()

    class SlowActiveManager:
        async def join_teardown(self, spawn_id):
            pass

        def __init__(self) -> None:
            self.stop_calls: list[dict[str, object]] = []
            self.producer: asyncio.Task[None] | None = None

        async def start_spawn(
            self,
            _config: ConnectionConfig,
            _spec: ResolvedLaunchSpec,
            *,
            event_hook=None,
        ) -> object:
            async def produce_events() -> None:
                for index in range(15):
                    await asyncio.sleep(0.05)
                    queue.put_nowait(
                        NormalizedHarnessEvent(
                            raw=RawHarnessEvent(
                                event_type="assistant/analysis",
                                harness_id=HarnessId.CURSOR.value,
                                payload={"index": index},
                            ),
                            semantics=EventSemantics(activity="turn_active"),
                        )
                    )
                completion.set()
                queue.put_nowait(None)

            self.producer = asyncio.create_task(produce_events())
            return type("Connection", (), {"subprocess_pid": None})()

        def raw_terminal_frames_are_authoritative(self, _spawn_id: SpawnId) -> bool:
            return False

        async def start_heartbeat(self, _spawn_id: SpawnId) -> None:
            return None

        def subscribe(
            self,
            _spawn_id: SpawnId,
        ) -> asyncio.Queue[NormalizedHarnessEvent | None]:
            return queue

        async def wait_for_completion(self, _spawn_id: SpawnId) -> DrainOutcome:
            await completion.wait()
            return DrainOutcome(status="succeeded", exit_code=0)

        def unsubscribe(self, _spawn_id: SpawnId) -> None:
            return None

        def get_connection(self, _spawn_id: SpawnId) -> None:
            return None

        async def stop_spawn(self, spawn_id: SpawnId, **kwargs: object) -> None:
            # Real SpawnManager only joins teardown after terminal publication;
            # it does not send another cancellation to a completed session.
            if not completion.is_set():
                self.stop_calls.append({"spawn_id": spawn_id, **kwargs})

    manager = SlowActiveManager()
    monkeypatch.setattr(streaming_runner_module, "CURSOR_INACTIVITY_TIMEOUT_SECONDS", 0.5)
    run = Spawn(
        spawn_id=SpawnId("fresh-events"),
        prompt="hello",
        model=ModelId("composer-2.5"),
        status="running",
    )

    attempt = await asyncio.wait_for(
        streaming_runner_module._run_streaming_attempt(
            run=run,
            runtime_root=tmp_path,
            launch_mode=FOREGROUND_LAUNCH_MODE,
            log_dir=tmp_path / "logs",
            manager=manager,  # type: ignore[arg-type]
            config=ConnectionConfig(
                spawn_id=run.spawn_id,
                harness_id=HarnessId.CURSOR,
                prompt=run.prompt,
                control_root=tmp_path,
                child_env={},
            ),
            run_spec=ResolvedLaunchSpec(
                model=str(run.model),
                harness=HarnessId.CURSOR,
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            budget_tracker=None,
            signal_event=asyncio.Event(),
            received_signal=[None],
            timeout_seconds=None,
            startup_timeout_seconds=0.1,
            event_observer=None,
            stream_stdout_to_terminal=False,
            lifecycle_service=_LifecycleRecorder(),  # type: ignore[arg-type]
        ),
        timeout=3.0,
    )
    if manager.producer is not None:
        await manager.producer

    assert attempt.drain_exit_code == 0
    assert attempt.terminated_by_inactivity is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_execute_with_streaming_succeeds_after_report_watchdog_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
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
    assert row.terminal.exit_code == 0
    assert row.terminal.error is None
    assert fake_heartbeat.touches
    report = (runtime_root / "spawns" / str(run.spawn_id) / "report.md").read_text(encoding="utf-8")
    assert "Watchdog fallback completed." in report


@pytest.mark.asyncio
async def test_execute_with_streaming_finalizes_when_duration_clock_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
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
    assert row.terminal.exit_code == 0
    assert row.terminal.error is None
    assert row.terminal.duration_secs == 0.0
    assert fake_heartbeat.touches
