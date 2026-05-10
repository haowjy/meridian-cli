from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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
from meridian.lib.launch.process.ports import ChildStartedHook, LaunchedProcess, ProcessLauncher
from meridian.lib.launch.process.primary_attach import (
    PortBindError,
    PrimaryAttachError,
    PrimaryAttachLauncher,
)
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _build_config(*, spawn_id: SpawnId, project_root: Path, ws_port: int = 0) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CODEX,
        prompt="hello",
        project_root=project_root,
        env_overrides={},
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
    ) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._events = events
        self._session_id = session_id
        self._subprocess_pid = subprocess_pid
        self._port_bind_failures = port_bind_failures
        self._stop_event = asyncio.Event()
        self.stop_called = False
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
        return HarnessId.CODEX

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

    async def stop(self) -> None:
        self.stop_called = True
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

    def launch(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: ChildStartedHook | None = None,
    ) -> LaunchedProcess:
        _ = (cwd, env, output_log_path)
        self.launch_commands.append(command)
        self.output_log_paths.append(output_log_path)
        metadata_path = self.spawn_dir / PRIMARY_META_FILENAME
        assert metadata_path.exists()
        self.metadata_seen_at_launch = cast(
            "dict[str, object]",
            json.loads(metadata_path.read_text(encoding="utf-8")),
        )
        if on_child_started is not None:
            on_child_started(self.pid)
        time.sleep(self.pause_seconds)
        return LaunchedProcess(exit_code=self.exit_code, pid=self.pid)


def _read_metadata(spawn_dir: Path) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((spawn_dir / PRIMARY_META_FILENAME).read_text(encoding="utf-8")),
    )


def _read_history_lines(spawn_dir: Path) -> list[dict[str, object]]:
    lines = (spawn_dir / HISTORY_FILENAME).read_text(encoding="utf-8").splitlines()
    return [cast("dict[str, object]", json.loads(line)) for line in lines if line.strip()]


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
        config=_build_config(spawn_id=SpawnId("p900"), project_root=tmp_path, ws_port=7811),
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
async def test_primary_attach_captures_session_id_from_connection(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p901"
    connection = FakeManagedConnection(events=[], session_id="thread-from-transport")
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p901"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
    )

    outcome = await launcher.run(
        config=_build_config(spawn_id=SpawnId("p901"), project_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    assert outcome.session_id == "thread-from-transport"
    assert _read_metadata(spawn_dir)["harness_session_id"] == "thread-from-transport"


@pytest.mark.asyncio
async def test_primary_attach_calls_on_running_when_tui_starts(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p901-running"
    connection = FakeManagedConnection(events=[])
    process_launcher = FakeProcessLauncher(spawn_dir=spawn_dir, pid=5151, pause_seconds=0.03)
    running_pids: list[int] = []
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p901-running"),
        spawn_dir=spawn_dir,
        connection=connection,
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=process_launcher,
        on_running=running_pids.append,
    )

    outcome = await launcher.run(
        config=_build_config(spawn_id=SpawnId("p901-running"), project_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    assert running_pids == [5151]
    assert outcome.tui_pid == 5151
    assert _read_metadata(spawn_dir)["tui_pid"] == 5151


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
        config=_build_config(spawn_id=SpawnId("p902"), project_root=tmp_path),
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
async def test_primary_attach_activity_transitions_update_metadata(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p903"
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

    activity_writes: list[str] = []
    original_write_metadata = launcher._write_metadata

    def _recording_write_metadata() -> None:
        activity_writes.append(launcher._metadata.activity)
        original_write_metadata()

    launcher._write_metadata = _recording_write_metadata  # type: ignore[method-assign]

    await launcher.run(
        config=_build_config(spawn_id=SpawnId("p903"), project_root=tmp_path),
        spec=_build_spec(),
        cwd=tmp_path,
        env={},
    )

    deduped_activity_writes: list[str] = []
    for activity in activity_writes:
        if deduped_activity_writes and deduped_activity_writes[-1] == activity:
            continue
        deduped_activity_writes.append(activity)

    assert deduped_activity_writes == [
        "starting",
        "idle",
        "turn_active",
        "idle",
        "finalizing",
    ]
    assert activity_writes[-1] == "finalizing"
    assert _read_metadata(spawn_dir)["activity"] == "finalizing"


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
            config=_build_config(spawn_id=SpawnId("p904"), project_root=tmp_path, ws_port=29000),
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
                    project_root=tmp_path,
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


def test_primary_attach_finalizing_is_terminal_activity_state(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p906"
    spawn_dir.mkdir(parents=True, exist_ok=True)
    launcher = PrimaryAttachLauncher(
        spawn_id=SpawnId("p906"),
        spawn_dir=spawn_dir,
        connection=FakeManagedConnection(events=[]),
        tui_command_builder=lambda session_id: ("codex", "resume", session_id),
        process_launcher=FakeProcessLauncher(spawn_dir=spawn_dir),
    )

    launcher._set_activity("finalizing")
    launcher._update_activity_from_event(
        HarnessEvent(
            event_type="turn/started",
            payload={"turnId": "late-turn"},
            harness_id="codex",
        )
    )

    assert launcher._metadata.activity == "finalizing"
