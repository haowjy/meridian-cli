from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.streaming_runner import (
    _inactivity_watchdog,
    _run_streaming_attempt,
)
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.spawn.model import FOREGROUND_LAUNCH_MODE
from meridian.lib.streaming.spawn_session import DrainOutcome


@dataclass
class _FakeConnection:
    subprocess_pid: int = 4242
    primary_event_scope: object | None = None


@dataclass
class _FakeLifecycleService:
    calls: list[str] = field(default_factory=list)

    def mark_running(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("mark_running")

    def record_exited(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("record_exited")


class _FakeManager:
    def __init__(self) -> None:
        self.stop_calls: list[dict[str, Any]] = []

    async def start_spawn(
        self,
        _config: ConnectionConfig,
        _spec: ResolvedLaunchSpec,
    ) -> _FakeConnection:
        return _FakeConnection()

    def raw_terminal_frames_are_authoritative(self, _spawn_id: SpawnId) -> bool:
        return False

    async def start_heartbeat(self, _spawn_id: SpawnId) -> None:
        return None

    def subscribe(self, _spawn_id: SpawnId) -> asyncio.Queue[HarnessEvent | None]:
        queue: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    async def wait_for_completion(self, _spawn_id: SpawnId) -> DrainOutcome:
        await asyncio.sleep(0.05)
        return DrainOutcome(status="succeeded", exit_code=0, duration_secs=0.1)

    def unsubscribe(self, _spawn_id: SpawnId) -> None:
        return None

    def get_connection(self, _spawn_id: SpawnId) -> object | None:
        return None

    async def stop_spawn(self, spawn_id: SpawnId, **kwargs: object) -> None:
        self.stop_calls.append({"spawn_id": spawn_id, **kwargs})


@pytest.mark.asyncio
async def test_startup_watchdog_reports_phase_and_cancels_start(
    tmp_path: Path,
) -> None:
    child_stopped = asyncio.Event()
    start_entered = asyncio.Event()

    class HangingStartupManager(_FakeManager):
        async def start_spawn(
            self,
            _config: ConnectionConfig,
            _spec: ResolvedLaunchSpec,
        ) -> _FakeConnection:
            start_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_stopped.set()
            raise AssertionError("unreachable")

    manager = HangingStartupManager()
    run = Spawn(
        spawn_id=SpawnId("p-startup-timeout"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="running",
    )
    config = ConnectionConfig(
        spawn_id=run.spawn_id,
        harness_id=HarnessId.CODEX,
        prompt=run.prompt,
        control_root=tmp_path,
        env_overrides={},
    )

    attempt = await _run_streaming_attempt(
        run=run,
        runtime_root=tmp_path,
        launch_mode=FOREGROUND_LAUNCH_MODE,
        log_dir=tmp_path / "logs",
        manager=manager,
        config=config,
        run_spec=ResolvedLaunchSpec(
            model="gpt-5.3-codex",
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
        lifecycle_service=_FakeLifecycleService(),  # type: ignore[arg-type]
    )

    assert start_entered.is_set()
    assert attempt.start_error == "startup phase timeout after 0.010s"
    assert child_stopped.is_set()


@pytest.mark.asyncio
async def test_inactivity_watchdog_stops_stale_spawn() -> None:
    manager = _FakeManager()
    completion_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    last_event_at = [loop.time() - 1.0]

    stopped = await _inactivity_watchdog(
        last_event_at=last_event_at,
        completion_event=completion_event,
        manager=manager,
        spawn_id=SpawnId("p-stall"),
        timeout_seconds=0.05,
        poll_seconds=0.01,
    )

    assert stopped is True
    assert len(manager.stop_calls) == 1
    assert manager.stop_calls[0]["error"] == "inactivity_stall"
    assert manager.stop_calls[0]["status"] == "failed"
    assert manager.stop_calls[0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_inactivity_watchdog_noop_when_completion_event_set_first() -> None:
    manager = _FakeManager()
    completion_event = asyncio.Event()
    completion_event.set()
    last_event_at = [asyncio.get_running_loop().time() - 100.0]

    stopped = await _inactivity_watchdog(
        last_event_at=last_event_at,
        completion_event=completion_event,
        manager=manager,
        spawn_id=SpawnId("p-done"),
        timeout_seconds=0.05,
        poll_seconds=0.01,
    )

    assert stopped is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_inactivity_watchdog_noop_while_events_keep_arriving() -> None:
    manager = _FakeManager()
    completion_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    last_event_at = [loop.time()]

    async def _refresh_activity() -> None:
        while not completion_event.is_set():
            last_event_at[0] = loop.time()
            await asyncio.sleep(0.02)

    async def _complete_soon() -> None:
        await asyncio.sleep(0.08)
        completion_event.set()

    refresh_task = asyncio.create_task(_refresh_activity())
    complete_task = asyncio.create_task(_complete_soon())
    try:
        stopped = await _inactivity_watchdog(
            last_event_at=last_event_at,
            completion_event=completion_event,
            manager=manager,
            spawn_id=SpawnId("p-active"),
            timeout_seconds=0.2,
            poll_seconds=0.01,
        )
    finally:
        await asyncio.gather(refresh_task, complete_task)

    assert stopped is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_non_cursor_harness_skips_inactivity_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inactivity_invocations: list[dict[str, object]] = []

    async def _track_inactivity(**kwargs: object) -> bool:
        inactivity_invocations.append(kwargs)
        return False

    async def _instant_report_watchdog(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "meridian.lib.launch.streaming_runner._inactivity_watchdog",
        _track_inactivity,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.streaming_runner._report_watchdog",
        _instant_report_watchdog,
    )

    manager = _FakeManager()
    lifecycle = _FakeLifecycleService()
    run = Spawn(
        spawn_id=SpawnId("p-codex"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="running",
    )
    config = ConnectionConfig(
        spawn_id=run.spawn_id,
        harness_id=HarnessId.CODEX,
        prompt=run.prompt,
        control_root=tmp_path,
        env_overrides={},
    )

    await _run_streaming_attempt(
        run=run,
        runtime_root=tmp_path,
        launch_mode=FOREGROUND_LAUNCH_MODE,
        log_dir=tmp_path / "logs",
        manager=manager,
        config=config,
        run_spec=ResolvedLaunchSpec(
            model="gpt-5.3-codex",
            harness=HarnessId.CODEX,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        budget_tracker=None,
        signal_event=asyncio.Event(),
        received_signal=[None],
        timeout_seconds=None,
        event_observer=None,
        stream_stdout_to_terminal=False,
        lifecycle_service=lifecycle,  # type: ignore[arg-type]
    )

    assert inactivity_invocations == []


@pytest.mark.asyncio
async def test_cursor_harness_arms_inactivity_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inactivity_invocations: list[dict[str, object]] = []

    async def _track_inactivity(**kwargs: object) -> bool:
        inactivity_invocations.append(kwargs)
        return False

    async def _instant_report_watchdog(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        "meridian.lib.launch.streaming_runner._inactivity_watchdog",
        _track_inactivity,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.streaming_runner._report_watchdog",
        _instant_report_watchdog,
    )

    manager = _FakeManager()
    lifecycle = _FakeLifecycleService()
    run = Spawn(
        spawn_id=SpawnId("p-cursor"),
        prompt="hello",
        model=ModelId("composer-2.5"),
        status="running",
    )
    config = ConnectionConfig(
        spawn_id=run.spawn_id,
        harness_id=HarnessId.CURSOR,
        prompt=run.prompt,
        control_root=tmp_path,
        env_overrides={},
    )

    await _run_streaming_attempt(
        run=run,
        runtime_root=tmp_path,
        launch_mode=FOREGROUND_LAUNCH_MODE,
        log_dir=tmp_path / "logs",
        manager=manager,
        config=config,
        run_spec=ResolvedLaunchSpec(
            model="composer-2.5",
            harness=HarnessId.CURSOR,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        budget_tracker=None,
        signal_event=asyncio.Event(),
        received_signal=[None],
        timeout_seconds=None,
        event_observer=None,
        stream_stdout_to_terminal=False,
        lifecycle_service=lifecycle,  # type: ignore[arg-type]
    )

    assert len(inactivity_invocations) == 1
    assert inactivity_invocations[0]["spawn_id"] == run.spawn_id
