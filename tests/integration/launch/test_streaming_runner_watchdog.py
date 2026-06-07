# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Streaming runner watchdog and lifecycle tests: cleanup, setup failure, and duration guard."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import constants as launch_constants
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    ExecutionBudget,
    LaunchArgvIntent,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.platform.process_scope import fallback as process_scope_fallback
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import (
    resolve_project_runtime_root,
    resolve_spawn_log_dir,
)
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.support.fakes import FakeClock, FakeHeartbeat
from tests.support.pi_extensions import configure_pi_extension_projection

streaming_runner_module = importlib.import_module("meridian.lib.launch.streaming_runner")


@pytest.fixture(autouse=True)
def _pi_extension_projection_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)


class _FakeControlSocketServer:
    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: object) -> None:
        _ = spawn_id, manager
        self.socket_path = socket_path

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None


class _ReportThenHangConnection:
    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "thread-watchdog"
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="interrupt_restart",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
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
        return 4242

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text(
            "# Done\n\nWatchdog fallback completed.\n",
            encoding="utf-8",
        )
        yield HarnessEvent(
            event_type="item/completed",
            harness_id="codex",
            payload={
                "item": {"id": "msg-1", "type": "agentMessage", "text": "done"},
                "threadId": self._session_id,
                "turnId": "turn-1",
            },
        )
        while True:
            await asyncio.sleep(3600)


class _PiTimeoutThenTerminalConnection:
    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "session-pi-timeout-race"
        self._stop_event = asyncio.Event()
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.PI

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return 5252

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        self._stop_event.set()
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text("# Report\n\nOK\n", encoding="utf-8")
        yield HarnessEvent(
            event_type="session",
            harness_id="pi",
            payload={"type": "session", "id": self._session_id},
        )
        yield HarnessEvent(
            event_type="turn_start",
            harness_id="pi",
            payload={"type": "turn_start"},
        )
        await self._stop_event.wait()
        yield HarnessEvent(
            event_type="agent_end",
            harness_id="pi",
            payload={
                "type": "agent_end",
                "messages": [
                    {"role": "assistant", "stopReason": "stop"},
                ],
            },
        )


class _PiTimeoutWithoutTerminalConnection:
    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "session-pi-timeout-no-terminal"
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.PI

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return 6262

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        yield HarnessEvent(
            event_type="session",
            harness_id="pi",
            payload={"type": "session", "id": self._session_id},
        )
        while True:
            await asyncio.sleep(3600)


class _OpenCodeTerminalWithScopeConnection:
    harness = HarnessId.OPENCODE
    terminal_event_type = "session.idle"
    terminal_payload_session_key = "sessionID"

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "session-opencode-cleanup"
        self._scope_snapshot: ProcessScopeSnapshot | None = ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id="",
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=73737,
            root_created_at_epoch=12_345.0,
            pgid=None,
            job_name=None,
            degraded_reason=None,
        )
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="http_post",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return self.harness

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return self._scope_snapshot.root_pid if self._scope_snapshot is not None else None

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        return self._scope_snapshot

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        current_scope = self._scope_snapshot
        assert current_scope is not None
        self._scope_snapshot = ProcessScopeSnapshot(
            scope_id=current_scope.scope_id,
            owner_policy=current_scope.owner_policy,
            owner_id=str(config.spawn_id),
            role=current_scope.role,
            containment=current_scope.containment,
            root_pid=current_scope.root_pid,
            root_created_at_epoch=current_scope.root_created_at_epoch,
            pgid=current_scope.pgid,
            job_name=current_scope.job_name,
            degraded_reason=current_scope.degraded_reason,
        )
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        scope = self._scope_snapshot
        if scope is not None:
            process_scope_fallback.terminate_tree_sync(
                pid=scope.root_pid,
                created_at_epoch=scope.root_created_at_epoch,
                grace_secs=5.0,
                reason="stop_called",
                scope_id=scope.scope_id,
            )
        self.state = "stopped"
        self._scope_snapshot = None
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text("# Done\n\nOpenCode completed.\n", encoding="utf-8")
        yield HarnessEvent(
            event_type=self.terminal_event_type,
            harness_id=self.harness.value,
            payload={
                "type": self.terminal_event_type,
                self.terminal_payload_session_key: self._session_id,
            },
        )


class _CodexTerminalWithScopeConnection(_OpenCodeTerminalWithScopeConnection):
    harness = HarnessId.CODEX
    terminal_event_type = "turn/completed"
    terminal_payload_session_key = "threadId"


class _EndMonotonicFailsClock(FakeClock):
    def __init__(self, start: float = 0.0) -> None:
        super().__init__(start=start)
        self._monotonic_reads = 0

    def monotonic(self) -> float:
        self._monotonic_reads += 1
        if self._monotonic_reads >= 2:
            raise RuntimeError("end monotonic unavailable")
        return super().monotonic()


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
    state_path = runtime_root / "spawns" / "p-child" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"id": "p-child", "parent_id": "p-parent", "status": "running"}),
        encoding="utf-8",
    )

    assert streaming_runner_module._retry_blocked_after_pi_child_started(
        harness_id=HarnessId.PI,
        runtime_root=runtime_root,
        current_spawn_id=SpawnId("p-parent"),
    )


def _build_request() -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )


def _build_pi_timeout_request(timeout_secs: int) -> SpawnRequest:
    return SpawnRequest(
        model="openai-codex/gpt-5.4-mini",
        harness=HarnessId.PI.value,
        prompt="Reply with exactly: OK",
        budget=ExecutionBudget(timeout_secs=timeout_secs),
    )


def _build_opencode_request() -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.OPENCODE.value,
        prompt="hello",
    )


async def _execute_with_context(
    run: Spawn,
    *,
    request: SpawnRequest,
    project_root: Path,
    runtime_root: Path,
    artifacts: LocalStore,
    registry: HarnessRegistry,
    **kwargs: object,
) -> int:
    launch_context = build_launch_context(
        spawn_id=str(run.spawn_id),
        request=request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.resolve().as_posix(),
        ),
        harness_registry=registry,
    )
    return await streaming_runner_module.execute_with_streaming(
        run,
        request=request,
        launch_context=launch_context,
        project_root=project_root,
        runtime_root=runtime_root,
        artifacts=artifacts,
        **kwargs,
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
@pytest.mark.parametrize(
    ("harness_id", "connection_cls", "spawn_request"),
    (
        (HarnessId.OPENCODE, _OpenCodeTerminalWithScopeConnection, _build_opencode_request()),
        (HarnessId.CODEX, _CodexTerminalWithScopeConnection, _build_request()),
    ),
)
async def test_execute_with_streaming_routes_backend_cleanup_through_stop_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness_id: HarnessId,
    connection_cls: type[_OpenCodeTerminalWithScopeConnection],
    spawn_request: SpawnRequest,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)
    cleanup_calls: list[tuple[int, float, float, str, str | None]] = []

    def _fake_terminate_tree_sync(
        *,
        pid: int,
        created_at_epoch: float,
        grace_secs: float,
        reason: str,
        scope_id: str,
    ) -> object:
        _ = scope_id
        row = spawn_store.get_spawn(runtime_root, run.spawn_id)
        cleanup_calls.append(
            (
                pid,
                created_at_epoch,
                grace_secs,
                reason,
                row.status if row is not None else None,
            )
        )
        return object()

    monkeypatch.setattr(process_scope_fallback, "terminate_tree_sync", _fake_terminate_tree_sync)
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    def _get_connection_class(
        _harness_id: HarnessId,
        _transport_id: TransportId = TransportId.STREAMING,
    ) -> type[_OpenCodeTerminalWithScopeConnection]:
        return connection_cls

    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        _get_connection_class,
    )

    run = Spawn(
        spawn_id=SpawnId(f"r-{harness_id.value}-cleanup"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex" if harness_id is HarnessId.CODEX else "gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id=f"test-chat-{harness_id.value}-cleanup",
        model=str(run.model),
        agent="",
        harness=harness_id.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=spawn_request,
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
    assert cleanup_calls == [(73737, 12_345.0, 5.0, "stop_called", "running")]
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "succeeded"


@pytest.mark.asyncio
async def test_execute_with_streaming_does_not_run_streaming_runner_scope_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root(tmp_path)
    artifacts = LocalStore(root_dir=tmp_path / ".artifacts")
    registry = HarnessRegistry.with_defaults()
    fake_clock = FakeClock(start=1_000.0)
    fake_heartbeat = FakeHeartbeat()
    fake_heartbeat.set_clock(fake_clock)

    def _fail_if_runner_cleanup_exists(
        scope: ProcessScopeSnapshot,
        *,
        grace_seconds: float,
        reason: str,
    ) -> object:
        _ = scope, grace_seconds, reason
        raise AssertionError("streaming_runner must not own backend scope cleanup")

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    def _get_connection_class(
        _harness_id: HarnessId,
        _transport_id: TransportId = TransportId.STREAMING,
    ) -> type[_OpenCodeTerminalWithScopeConnection]:
        return _OpenCodeTerminalWithScopeConnection

    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        _get_connection_class,
    )
    monkeypatch.setattr(
        streaming_runner_module,
        "terminate_scope_sync",
        _fail_if_runner_cleanup_exists,
        raising=False,
    )

    run = Spawn(
        spawn_id=SpawnId("r-opencode-cleanup"),
        prompt="hello",
        model=ModelId("gpt-5.4"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-opencode-cleanup",
        model=str(run.model),
        agent="",
        harness=HarnessId.OPENCODE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=_build_opencode_request(),
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


@pytest.mark.asyncio
async def test_setup_failure_produces_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When setup raises, execute_with_streaming still writes a terminal event."""
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

    run = Spawn(
        spawn_id=SpawnId("r-setup-fail"),
        prompt="hello",
        model=ModelId("gpt-5.3-codex"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-setup-fail",
        model=str(run.model),
        agent="",
        harness=HarnessId.CODEX.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    original_update = spawn_store.update_spawn
    call_count = 0

    def _exploding_update(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("Injected setup failure")
        original_update(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "update_spawn", _exploding_update)

    exit_code = await _execute_with_context(
        run,
        request=_build_request(),
        project_root=tmp_path,
        runtime_root=runtime_root,
        artifacts=artifacts,
        registry=registry,
        clock=fake_clock,
        heartbeat_touch=fake_heartbeat.touch,
    )

    assert exit_code == launch_constants.DEFAULT_INFRA_EXIT_CODE
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    assert row.status == "failed"
    assert row.exit_code == launch_constants.DEFAULT_INFRA_EXIT_CODE
    assert row.error is not None
    assert row.terminal_origin == "runner"


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
