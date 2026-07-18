from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
    ObserverEndpoint,
)
from meridian.lib.launch.constants import HISTORY_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import primary_attach as primary_attach_module
from meridian.lib.launch.process.ports import LaunchedProcess, ProcessLauncher
from meridian.lib.launch.process.primary_attach import (
    PortBindError,
    PrimaryAttachError,
    PrimaryAttachLauncher,
)
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.platform.process_scope.base import PROCESS_BIRTH_UNKNOWN_EPOCH
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.process_scope_projection import read_scopes_from_disk, record_scope
from meridian.lib.state.spawn_store import start_spawn

_BACKEND_SCOPE_EPOCH = 12_345.0


def _publish_spawn(spawn_dir: Path) -> None:
    start_spawn(
        spawn_dir.parent.parent,
        spawn_id=spawn_dir.name,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="tester",
        harness="codex",
        prompt="test",
        status="running",
    )


def _build_config(
    *,
    spawn_id: SpawnId,
    control_root: Path,
    ws_port: int = 0,
    harness_id: HarnessId = HarnessId.CODEX,
) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=harness_id,
        prompt="hello",
        control_root=control_root,
        child_env={},
        ws_port=ws_port,
    )


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        interactive=True,
    )


class FakeManagedConnection:
    def __init__(
        self,
        *,
        events: list[HarnessEvent],
        session_id: str = "thread-123",
        subprocess_pid: int = 913,
        port_bind_failures: int = 0,
        harness_id: HarnessId = HarnessId.CODEX,
    ) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._harness_id = harness_id
        self._events = events
        self._session_id = session_id
        self._subprocess_pid = subprocess_pid
        self._port_bind_failures = port_bind_failures
        self._stop_event = asyncio.Event()
        self.stop_called = False
        self.stop_reasons: list[str | None] = []
        self.started_primary_observer_mode: bool | None = None
        self.started_ports: list[int] = []
        self.start_calls = 0
        self._observer_endpoint: ObserverEndpoint | None = None
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="interrupt_restart",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
            supports_primary_observer=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return self._harness_id

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return self._subprocess_pid

    @property
    def primary_event_scope(self) -> None:
        return None

    @property
    def managed_backend(self) -> None:
        return None

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        if self._subprocess_pid <= 0:
            return None
        return ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id=str(self._spawn_id),
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=self._subprocess_pid,
            root_created_at_epoch=_BACKEND_SCOPE_EPOCH,
            pgid=None,
            job_name=None,
            degraded_reason=None,
        )

    @property
    def observer_endpoint(self) -> ObserverEndpoint | None:
        return self._observer_endpoint

    async def start(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        _ = spec
        self.start_calls += 1
        self.started_ports.append(config.ws_port)
        self._spawn_id = config.spawn_id
        start_in_observer_mode = self.started_primary_observer_mode is True
        if start_in_observer_mode and config.ws_port > 0:
            self._observer_endpoint = ObserverEndpoint(
                transport="ws",
                url=f"ws://{config.ws_bind_host}:{config.ws_port}",
                host=config.ws_bind_host,
                port=config.ws_port,
            )
        else:
            self._observer_endpoint = None
        if self.start_calls <= self._port_bind_failures:
            self.state = "failed"
            raise PortBindError("address already in use (test)")
        self.state = "connected"

    async def start_observer(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        self.started_primary_observer_mode = True
        await self.start(config, spec)

    async def stop(self, *, reason: str | None = None) -> None:
        self.stop_called = True
        self.stop_reasons.append(reason)
        self.state = "stopped"
        self._stop_event.set()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event
            await asyncio.sleep(0)
        await self._stop_event.wait()


@dataclass
class FakeProcessLauncher(ProcessLauncher):
    spawn_dir: Path
    pid: int = 4242
    exit_code: int = 0
    pause_seconds: float = 0.05
    launch_commands: list[tuple[str, ...]] = field(default_factory=list)
    output_log_paths: list[Path | None] = field(default_factory=list)
    metadata_seen_at_launch: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _publish_spawn(self.spawn_dir)

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
    ) -> FakeProcessLauncher:
        _ = (cwd, env, output_log_path)
        self.launch_commands.append(command)
        self.output_log_paths.append(output_log_path)
        metadata_path = self.spawn_dir / PRIMARY_META_FILENAME
        assert metadata_path.exists()
        self.metadata_seen_at_launch = cast(
            "dict[str, object]",
            json.loads(metadata_path.read_text(encoding="utf-8")),
        )
        return self

    def wait(self) -> LaunchedProcess:
        time.sleep(self.pause_seconds)
        return LaunchedProcess(exit_code=self.exit_code, pid=self.pid)

    def terminate(self) -> None:
        return None

    def cancel_wait(self) -> None:
        return None


@dataclass
class BlockingProcessLauncher(ProcessLauncher):
    spawn_dir: Path
    pid: int = 5252
    launch_commands: list[tuple[str, ...]] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)
    wait_started: threading.Event = field(default_factory=threading.Event)
    wait_finished: threading.Event = field(default_factory=threading.Event)
    wait_timeout_seconds: float | None = 5.0
    terminate_releases: bool = True
    cancel_wait_releases: bool = True
    cancel_wait_calls: int = 0

    def __post_init__(self) -> None:
        _publish_spawn(self.spawn_dir)

    def start(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
    ) -> BlockingProcessLauncher:
        _ = (cwd, env, output_log_path)
        self.launch_commands.append(command)
        metadata_path = self.spawn_dir / PRIMARY_META_FILENAME
        assert metadata_path.exists()
        return self

    def wait(self) -> LaunchedProcess:
        self.wait_started.set()
        try:
            self.release.wait(timeout=self.wait_timeout_seconds)
            return LaunchedProcess(exit_code=143, pid=self.pid)
        finally:
            self.wait_finished.set()

    def terminate(self) -> None:
        if self.terminate_releases:
            self.release.set()

    def cancel_wait(self) -> None:
        self.cancel_wait_calls += 1
        if self.cancel_wait_releases:
            self.release.set()


class FailingEventConnection(FakeManagedConnection):
    def __init__(
        self,
        *,
        fail_after_started: asyncio.Event,
        stream_closed: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._fail_after_started = fail_after_started
        self._stream_closed = stream_closed

    async def events(self):  # type: ignore[no-untyped-def]
        await self._fail_after_started.wait()
        self.state = "failed"
        if self._stream_closed is not None:
            self._stream_closed.set()
        if False:
            yield HarnessEvent(event_type="unreachable", payload={}, harness_id="opencode")


def _read_metadata(spawn_dir: Path) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((spawn_dir / PRIMARY_META_FILENAME).read_text(encoding="utf-8")),
    )


def _read_history_lines(spawn_dir: Path) -> list[dict[str, object]]:
    lines = (spawn_dir / HISTORY_FILENAME).read_text(encoding="utf-8").splitlines()
    return [cast("dict[str, object]", json.loads(line)) for line in lines if line.strip()]


def test_primary_attach_scope_snapshot_records_unknown_birth_sentinel_when_create_time_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InaccessibleProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(primary_attach_module.psutil, "Process", _InaccessibleProcess)

    snapshot = primary_attach_module._make_scope_snapshot(
        pid=987_654_321,
        scope_id="tui",
        owner_policy="session_owned",
        owner_id="thread-unknown",
        role="primary_tui",
    )

    assert snapshot.root_created_at_epoch == PROCESS_BIRTH_UNKNOWN_EPOCH


@pytest.mark.asyncio
async def test_primary_attach_event_writer_failure_stops_connection_and_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p900-liveness"
    tui_started = asyncio.Event()
    connection = FailingEventConnection(
        fail_after_started=tui_started,
        events=[],
        session_id="sess-dead",
        harness_id=HarnessId.OPENCODE,
    )
    process_launcher = BlockingProcessLauncher(spawn_dir=spawn_dir)
    terminated_scopes: list[str] = []

    def _terminate_scope(scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = grace_seconds
        terminated_scopes.append(f"{scope.scope_id}:{reason}")
        process_launcher.release.set()
        return None

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p900-liveness"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("opencode", "attach", session_id),
        process_launcher=process_launcher,
        on_running=lambda _pid: tui_started.set(),
    )

    outcome = await launcher.run(
        config=_build_config(
            spawn_id=SpawnId("p900-liveness"),
            control_root=tmp_path,
            harness_id=HarnessId.OPENCODE,
        ),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    assert outcome.exit_code == 1
    assert connection.stop_reasons == ["event_stream_closed", None]
    assert terminated_scopes == ["tui:event_stream_closed"]
    assert _read_metadata(spawn_dir)["tui_pid"] == 5252


@pytest.mark.asyncio
async def test_primary_attach_cancellation_stops_blocking_tui_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p900-cancel"
    tui_started = asyncio.Event()
    connection = FakeManagedConnection(events=[], session_id="sess-cancel")
    process_launcher = BlockingProcessLauncher(spawn_dir=spawn_dir)
    terminated_scopes: list[str] = []

    def _terminate_scope(scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = grace_seconds
        terminated_scopes.append(f"{scope.scope_id}:{reason}")
        process_launcher.release.set()
        return None

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p900-cancel"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
        on_running=lambda _pid: tui_started.set(),
    )

    run_task = asyncio.create_task(
        launcher.run(
            config=_build_config(spawn_id=SpawnId("p900-cancel"), control_root=tmp_path),
            spec=_build_spec(),
            cwd=tmp_path,
            env={},
        )
    )
    await tui_started.wait()

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert connection.stop_reasons == [None]
    assert terminated_scopes == ["tui:primary_attach_shutdown"]
    assert _read_metadata(spawn_dir)["tui_pid"] == 5252


def test_primary_attach_cancellation_does_not_join_uncooperative_wait_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p901-cancel-timeout"
    connection = FakeManagedConnection(events=[], session_id="sess-cancel-timeout")
    process_launcher = BlockingProcessLauncher(
        spawn_dir=spawn_dir,
        wait_timeout_seconds=None,
        terminate_releases=False,
        cancel_wait_releases=False,
    )

    def _terminate_scope(_scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = (grace_seconds, reason)

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    monkeypatch.setattr(primary_attach_module, "_TUI_LAUNCH_DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(primary_attach_module, "_TUI_WAIT_CANCEL_TIMEOUT_SECONDS", 0.05)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p901-cancel-timeout"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    async def _cancel_after_tui_start() -> None:
        run_task = asyncio.create_task(
            launcher.run(
                config=_build_config(
                    spawn_id=SpawnId("p901-cancel-timeout"),
                    control_root=tmp_path,
                ),
                spec=_build_spec(),
                cwd=tmp_path,
                env={},
            )
        )
        while not process_launcher.wait_started.is_set():
            await asyncio.sleep(0)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    try:
        asyncio.run(_cancel_after_tui_start())

        assert process_launcher.wait_finished.is_set() is False
        assert process_launcher.cancel_wait_calls == 1
        assert process_launcher.release.is_set() is False
        assert connection.stop_reasons == [None]
    finally:
        process_launcher.release.set()


@pytest.mark.asyncio
async def test_primary_attach_codex_event_stream_closure_does_not_stop_backend_or_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p900-codex-stream"
    tui_started = asyncio.Event()
    stream_closed = asyncio.Event()
    connection = FailingEventConnection(
        fail_after_started=tui_started,
        stream_closed=stream_closed,
        events=[],
        session_id="sess-codex",
    )
    process_launcher = BlockingProcessLauncher(spawn_dir=spawn_dir)
    terminated_scopes: list[str] = []

    def _terminate_scope(scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = grace_seconds
        terminated_scopes.append(f"{scope.scope_id}:{reason}")
        return None

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p900-codex-stream"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
        on_running=lambda _pid: tui_started.set(),
    )

    run_task = asyncio.create_task(
        launcher.run(
            config=_build_config(spawn_id=SpawnId("p900-codex-stream"), control_root=tmp_path),
            spec=_build_spec(),
            cwd=tmp_path,
            env={},
        )
    )

    await stream_closed.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    assert connection.stop_reasons == []
    assert terminated_scopes == []

    process_launcher.release.set()
    outcome = await run_task

    assert outcome.exit_code == 143
    assert connection.stop_reasons == [None]
    assert terminated_scopes == []
    assert _read_metadata(spawn_dir)["tui_pid"] == 5252


def test_primary_attach_cancellation_after_codex_observer_displacement_cleans_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p902-codex-cancel"
    tui_started = asyncio.Event()
    stream_closed = asyncio.Event()
    connection = FailingEventConnection(
        fail_after_started=tui_started,
        stream_closed=stream_closed,
        events=[],
        session_id="sess-codex-cancel",
    )
    process_launcher = BlockingProcessLauncher(spawn_dir=spawn_dir)
    terminated_scopes: list[str] = []

    def _terminate_scope(scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = grace_seconds
        terminated_scopes.append(f"{scope.scope_id}:{reason}")

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    monkeypatch.setattr(primary_attach_module, "_TUI_LAUNCH_DRAIN_TIMEOUT_SECONDS", 0.05)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p902-codex-cancel"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
        on_running=lambda _pid: tui_started.set(),
    )

    async def _cancel_after_observer_displacement() -> None:
        run_task = asyncio.create_task(
            launcher.run(
                config=_build_config(
                    spawn_id=SpawnId("p902-codex-cancel"),
                    control_root=tmp_path,
                ),
                spec=_build_spec(),
                cwd=tmp_path,
                env={},
            )
        )
        await stream_closed.wait()
        async with asyncio.timeout(1.0):
            while launcher._event_writer_task is not None:
                await asyncio.sleep(0)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    asyncio.run(_cancel_after_observer_displacement())

    assert terminated_scopes == ["tui:primary_attach_shutdown"]
    assert process_launcher.release.is_set()
    assert connection.stop_reasons == [None]


@pytest.mark.asyncio
async def test_primary_attach_cancellation_during_opencode_drain_retries_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p903-opencode-cancel"
    tui_started = asyncio.Event()
    stream_closed = asyncio.Event()
    drain_started = asyncio.Event()
    connection = FailingEventConnection(
        fail_after_started=tui_started,
        stream_closed=stream_closed,
        events=[],
        session_id="sess-opencode-cancel",
        harness_id=HarnessId.OPENCODE,
    )
    process_launcher = BlockingProcessLauncher(
        spawn_dir=spawn_dir,
        terminate_releases=False,
    )
    terminated_scopes: list[str] = []

    def _terminate_scope(scope: Any, *, grace_seconds: float, reason: str) -> None:
        _ = grace_seconds
        terminated_scopes.append(f"{scope.scope_id}:{reason}")

    real_shield = asyncio.shield

    def _shield(awaitable: Any) -> asyncio.Future[Any]:
        drain_started.set()
        return real_shield(awaitable)

    monkeypatch.setattr(primary_attach_module, "terminate_scope_sync", _terminate_scope)
    monkeypatch.setattr(primary_attach_module.asyncio, "shield", _shield)
    monkeypatch.setattr(primary_attach_module, "_TUI_LAUNCH_DRAIN_TIMEOUT_SECONDS", 0.05)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p903-opencode-cancel"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("opencode", "attach", session_id),
        process_launcher=process_launcher,
        on_running=lambda _pid: tui_started.set(),
    )
    run_task = asyncio.create_task(
        launcher.run(
            config=_build_config(
                spawn_id=SpawnId("p903-opencode-cancel"),
                control_root=tmp_path,
                harness_id=HarnessId.OPENCODE,
            ),
            spec=_build_spec(),
            cwd=tmp_path,
            env={},
        )
    )

    await stream_closed.wait()
    await drain_started.wait()
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert terminated_scopes == [
        "tui:event_stream_closed",
        "tui:primary_attach_shutdown",
    ]
    assert process_launcher.release.is_set()
    assert process_launcher.cancel_wait_calls == 1
    assert connection.stop_reasons == ["event_stream_closed", None]


@pytest.mark.asyncio
async def test_primary_attach_writes_metadata_before_tui_launch(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p900"
    connection = FakeManagedConnection(events=[])
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)
    requested_sessions: list[str] = []

    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p900"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: (
            requested_sessions.append(session_id) or ("codex", "resume", session_id)
        ),
        process_launcher=process_launcher,
    )

    await launcher.run(
        config=_build_config(spawn_id=SpawnId("p900"), control_root=tmp_path, ws_port=7811),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    assert connection.started_primary_observer_mode is True
    assert requested_sessions == ["thread-123"]
    launch_meta = process_launcher.metadata_seen_at_launch
    assert launch_meta is not None
    assert launch_meta["activity"] == "idle"
    assert launch_meta["backend_pid"] == 913
    assert launch_meta["backend_port"] == 7811
    assert launch_meta["harness_session_id"] == "thread-123"
    assert process_launcher.output_log_paths == [None]


@pytest.mark.asyncio
async def test_primary_attach_upgrades_provisional_backend_scope_without_duplicate(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p900-scope")
    spawn_dir = tmp_path / "spawns" / str(spawn_id)
    connection = FakeManagedConnection(events=[], session_id="thread-upgraded")
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)
    provisional = connection.scope_snapshot
    assert provisional is not None
    record_scope(tmp_path, spawn_id, provisional)

    launcher = PrimaryAttachLauncher(
        spawn_id=spawn_id,
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    await launcher.run(
        config=_build_config(spawn_id=spawn_id, control_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    backend_scopes = [
        scope for scope in read_scopes_from_disk(tmp_path, spawn_id) if scope.scope_id == "backend"
    ]
    assert [(scope.owner_policy, scope.owner_id) for scope in backend_scopes] == [
        ("session_owned", "thread-upgraded")
    ]


@pytest.mark.asyncio
async def test_primary_attach_writes_valid_jsonl_events(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p902"
    connection = FakeManagedConnection(
        events=[
            HarnessEvent(
                event_type="turn/started",
                payload={"turnId": "t1"},
                harness_id="codex",
            ),
            HarnessEvent(
                event_type="turn/completed",
                payload={"turnId": "t1"},
                harness_id="codex",
            ),
        ]
    )
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir, pause_seconds=0.08)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p902"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    await launcher.run(
        config=_build_config(spawn_id=SpawnId("p902"), control_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    rows = _read_history_lines(spawn_dir)
    assert [row["event_type"] for row in rows] == ["turn/started", "turn/completed"]
    assert [row["turn_id"] for row in rows] == ["t1", "t1"]
    for row in rows:
        assert isinstance(row["payload"], dict)
        assert row["harness_id"] == "codex"
        assert isinstance(row["seq"], int)
        assert isinstance(row["byte_offset"], int)
        assert row["item_id"] is None
        assert row["request_id"] is None
        assert row["interrupt_epoch"] == 0
        assert row["stale_after_interrupt"] is False


@pytest.mark.asyncio
async def test_primary_attach_activity_transitions_update_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_dir = tmp_path / "spawns" / "p903"
    activity_transitions: list[object] = []
    original_write_primary_metadata = primary_attach_module.write_primary_metadata

    def _record_activity_write(spawn_dir_arg: Path, metadata: Any) -> None:
        if not activity_transitions or activity_transitions[-1] != metadata.activity:
            activity_transitions.append(metadata.activity)
        original_write_primary_metadata(spawn_dir_arg, metadata)

    monkeypatch.setattr(
        primary_attach_module,
        "write_primary_metadata",
        _record_activity_write,
    )
    connection = FakeManagedConnection(
        events=[
            HarnessEvent(
                event_type="turn/started",
                payload={"turnId": "turn-7"},
                harness_id="codex",
            ),
            HarnessEvent(
                event_type="turn/completed",
                payload={"turnId": "turn-7"},
                harness_id="codex",
            ),
        ]
    )
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir, pause_seconds=0.08)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p903"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    await launcher.run(
        config=_build_config(spawn_id=SpawnId("p903"), control_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    metadata = _read_metadata(spawn_dir)
    assert metadata["activity"] == "finalizing"
    assert "turn_active" in activity_transitions


@pytest.mark.asyncio
async def test_primary_attach_retries_port_bind_with_fresh_ports(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p904"
    connection = FakeManagedConnection(events=[], port_bind_failures=2)
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p904"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    retries = iter((29001, 29002))
    original_reserve_local_port = primary_attach_module._reserve_local_port

    def _reserve_retry_port(host: str) -> int:
        _ = host
        return next(retries)

    primary_attach_module._reserve_local_port = _reserve_retry_port
    try:
        outcome = await launcher.run(
            config=_build_config(spawn_id=SpawnId("p904"), control_root=tmp_path, ws_port=29000),
            spec=_build_spec(),
            cwd=tmp_path,
            env={},
        )
    finally:
        primary_attach_module._reserve_local_port = original_reserve_local_port

    assert outcome.exit_code == 0
    assert connection.start_calls == 3
    assert connection.started_ports == [29000, 29001, 29002]
    assert process_launcher.launch_commands == [("codex", "resume", "thread-123")]
    assert _read_metadata(spawn_dir)["backend_port"] == 29002


@pytest.mark.asyncio
async def test_primary_attach_raises_after_max_port_bind_retries(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p905"
    connection = FakeManagedConnection(events=[], port_bind_failures=3)
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p905"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    retries = iter((29101, 29102))
    original_reserve_local_port = primary_attach_module._reserve_local_port

    def _reserve_retry_port(host: str) -> int:
        _ = host
        return next(retries)

    primary_attach_module._reserve_local_port = _reserve_retry_port
    try:
        with pytest.raises(PrimaryAttachError, match="Port bind failed after 3 attempts"):
            await launcher.run(
                config=_build_config(
                    spawn_id=SpawnId("p905"),
                    control_root=tmp_path,
                    ws_port=29100,
                ),
                spec=_build_spec(),
                cwd=tmp_path,
                env={},
            )
    finally:
        primary_attach_module._reserve_local_port = original_reserve_local_port

    assert connection.start_calls == 3
    assert connection.started_ports == [29100, 29101, 29102]
    assert process_launcher.launch_commands == []
