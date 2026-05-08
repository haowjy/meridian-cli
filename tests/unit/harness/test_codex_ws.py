from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from meridian.lib.core.telemetry import StartupPhase
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness import ensure_bootstrap
from meridian.lib.harness.connections import codex_ws
from meridian.lib.harness.connections.base import (
    AutoAcceptHandler,
    ConnectionConfig,
    HarnessRequest,
    InteractiveHandler,
    PrimaryRuntimeRequestPolicy,
)
from meridian.lib.harness.launch_spec import CodexLaunchSpec
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.harness.projections.project_codex_common import (
    HarnessCapabilityMismatch,
    map_codex_approval_policy,
)
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
    project_codex_spec_to_thread_request,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.process import runner as process_runner
from meridian.lib.safety.permissions import (
    PermissionConfig,
    TieredPermissionResolver,
    UnsafeNoOpPermissionResolver,
)

CODEX_TURN_STARTED_EVENT = "turn/started"
CODEX_TURN_COMPLETED_EVENT = "turn/completed"
CODEX_THREAD_ACTIVITY_EVENTS = ("thread/start", "thread/started")


@pytest.fixture(autouse=True)
def _bootstrap_harness_registry() -> None:
    ensure_bootstrap()


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4321
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, _data: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _build_connection_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=SpawnId("p123"),
        harness_id=HarnessId.CODEX,
        prompt="hello from test",
        project_root=tmp_path,
        env_overrides={},
    )


def _values_for_setting(command: list[str], key: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(command):
        if token != "-c":
            continue
        if index + 1 >= len(command):
            continue
        setting = command[index + 1]
        prefix = f"{key}="
        if setting.startswith(prefix):
            values.append(setting[len(prefix) :])
    return values


def test_codex_ws_update_turn_state_tracks_started_and_completed_events() -> None:
    connection = codex_ws.CodexConnection()

    connection._update_turn_state(
        method=CODEX_TURN_STARTED_EVENT,
        payload={"turnId": "turn-activity"},
    )

    assert connection._current_turn_id == "turn-activity"
    assert connection._signal_in_flight is False

    connection._signal_in_flight = True
    connection._update_turn_state(
        method=CODEX_TURN_COMPLETED_EVENT,
        payload={"turnId": "turn-activity"},
    )

    assert connection._current_turn_id is None
    assert connection._signal_in_flight is False


@pytest.mark.parametrize("event_type", CODEX_THREAD_ACTIVITY_EVENTS)
def test_codex_ws_update_turn_state_accepts_thread_activity_aliases(event_type: str) -> None:
    connection = codex_ws.CodexConnection()

    connection._update_turn_state(
        method=event_type,
        payload={"thread": {"id": "thread-alias"}, "turn": {"id": "turn-alias"}},
    )

    assert connection._thread_id == "thread-alias"
    assert connection._current_turn_id == "turn-alias"


def test_codex_ws_capabilities_runtime_hitl_depends_on_request_handler() -> None:
    auto_accept_connection = codex_ws.CodexConnection(request_handler=AutoAcceptHandler())
    assert auto_accept_connection.capabilities.supports_runtime_hitl is False

    async def _event_sink(_event: object) -> None:
        return None

    interactive_connection = codex_ws.CodexConnection(
        request_handler=InteractiveHandler(_event_sink)
    )
    assert interactive_connection.capabilities.supports_runtime_hitl is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_start_observer", "is_fresh", "expect_turn_start"),
    (
        (False, True, True),   # spawn mode + fresh -> sends user prompt turn
        (True, True, True),    # primary observer + fresh -> sends bootstrap turn
        (True, False, False),  # primary observer + resume -> no turn (already has rollout)
    ),
)
async def test_codex_ws_start_respects_primary_observer_mode_for_initial_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    use_start_observer: bool,
    is_fresh: bool,
    expect_turn_start: bool,
) -> None:
    connection = codex_ws.CodexConnection()
    fake_process = _FakeProcess()
    request_methods: list[str] = []

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        return fake_process

    async def _fake_connect_with_retry(*_args: object, **_kwargs: object) -> _FakeWebSocket:
        return _FakeWebSocket()

    async def _fake_request(
        method: str,
        _params: dict[str, object],
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        _ = timeout_seconds
        request_methods.append(method)
        return {}

    async def _fake_notify(_method: str) -> None:
        return None

    async def _fake_bootstrap_thread(_spec: CodexLaunchSpec) -> dict[str, object]:
        return {"threadId": "thread-primary-observer"}

    async def _fake_read_messages_loop() -> None:
        return None

    async def _fake_wait_for_rollout_materialization(timeout_seconds: float = 120.0) -> None:
        return None

    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_request", _fake_request)
    monkeypatch.setattr(connection, "_notify", _fake_notify)
    monkeypatch.setattr(connection, "_bootstrap_thread", _fake_bootstrap_thread)
    monkeypatch.setattr(connection, "_read_messages_loop", _fake_read_messages_loop)
    monkeypatch.setattr(
        connection,
        "_wait_for_rollout_materialization",
        _fake_wait_for_rollout_materialization,
    )

    config = _build_connection_config(tmp_path)
    spec = CodexLaunchSpec(
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        continue_session_id="existing-thread" if not is_fresh else None,
    )
    if use_start_observer:
        await connection.start_observer(config, spec)
    else:
        await connection.start(config, spec)

    assert connection.session_id == "thread-primary-observer"
    assert ("turn/start" in request_methods) is expect_turn_start
    assert request_methods[0] == "initialize"

    await connection._cleanup_resources(mark_stopped=False)


@pytest.mark.asyncio
async def test_codex_ws_primary_observer_emits_startup_phases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = codex_ws.CodexConnection()
    fake_process = _FakeProcess()
    phases: list[StartupPhase] = []
    assert connection.capabilities.supported_startup_phases == frozenset(
        phase.value
        for phase in (
            StartupPhase.LAUNCHING_SUBPROCESS,
            StartupPhase.WAITING_FOR_CONNECTION,
            StartupPhase.INITIALIZING_SESSION,
            StartupPhase.HARNESS_READY,
            StartupPhase.HARNESS_FAILED,
        )
    )

    class _RecordingStartupPhaseEmitter:
        def __init__(
            self,
            spawn_id: str,
            *,
            harness_id: str = "",
            model: str | None = None,
            agent: str | None = None,
        ) -> None:
            assert spawn_id == "p123"
            assert harness_id == "codex"
            assert model == "gpt-test"
            assert agent is None

        def emit(self, phase: StartupPhase) -> None:
            phases.append(phase)

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        return fake_process

    async def _fake_connect_with_retry(*_args: object, **_kwargs: object) -> _FakeWebSocket:
        return _FakeWebSocket()

    async def _fake_request(
        method: str,
        _params: dict[str, object],
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        _ = timeout_seconds
        if method == "initialize":
            return {}
        if method == "turn/start":
            return {"turnId": "turn-bootstrap"}
        return {}

    async def _fake_notify(_method: str) -> None:
        return None

    async def _fake_bootstrap_thread(_spec: CodexLaunchSpec) -> dict[str, object]:
        return {"threadId": "thread-primary-observer"}

    async def _fake_read_messages_loop() -> None:
        return None

    async def _fake_wait_for_rollout_materialization(timeout_seconds: float = 120.0) -> None:
        _ = timeout_seconds
        return None

    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(codex_ws, "StartupPhaseEmitter", _RecordingStartupPhaseEmitter)
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_request", _fake_request)
    monkeypatch.setattr(connection, "_notify", _fake_notify)
    monkeypatch.setattr(connection, "_bootstrap_thread", _fake_bootstrap_thread)
    monkeypatch.setattr(connection, "_read_messages_loop", _fake_read_messages_loop)
    monkeypatch.setattr(
        connection,
        "_wait_for_rollout_materialization",
        _fake_wait_for_rollout_materialization,
    )

    config = ConnectionConfig(
        spawn_id=SpawnId("p123"),
        harness_id=HarnessId.CODEX,
        prompt="hello from test",
        project_root=tmp_path,
        env_overrides={},
    )
    spec = CodexLaunchSpec(
        model="gpt-test",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    await connection.start_observer(config, spec)

    assert phases == [
        StartupPhase.LAUNCHING_SUBPROCESS,
        StartupPhase.WAITING_FOR_CONNECTION,
        StartupPhase.INITIALIZING_SESSION,
        StartupPhase.INITIALIZING_SESSION,
        StartupPhase.HARNESS_READY,
    ]

    await connection._cleanup_resources(mark_stopped=False)


@pytest.mark.asyncio
async def test_codex_ws_non_observer_emits_startup_phases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = codex_ws.CodexConnection()
    fake_process = _FakeProcess()
    phases: list[StartupPhase] = []

    class _RecordingStartupPhaseEmitter:
        def __init__(self, _spawn_id: str, **_context: object) -> None:
            return None

        def emit(self, phase: StartupPhase) -> None:
            phases.append(phase)

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        return fake_process

    async def _fake_connect_with_retry(*_args: object, **_kwargs: object) -> _FakeWebSocket:
        return _FakeWebSocket()

    async def _fake_request(
        method: str,
        _params: dict[str, object],
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        _ = timeout_seconds
        if method == "turn/start":
            return {"turnId": "turn-1"}
        return {}

    async def _fake_notify(_method: str) -> None:
        return None

    async def _fake_bootstrap_thread(_spec: CodexLaunchSpec) -> dict[str, object]:
        return {"threadId": "thread-1"}

    async def _fake_read_messages_loop() -> None:
        return None

    monkeypatch.setattr(codex_ws.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(codex_ws, "StartupPhaseEmitter", _RecordingStartupPhaseEmitter)
    monkeypatch.setattr(connection, "_connect_with_retry", _fake_connect_with_retry)
    monkeypatch.setattr(connection, "_request", _fake_request)
    monkeypatch.setattr(connection, "_notify", _fake_notify)
    monkeypatch.setattr(connection, "_bootstrap_thread", _fake_bootstrap_thread)
    monkeypatch.setattr(connection, "_read_messages_loop", _fake_read_messages_loop)

    await connection.start(
        _build_connection_config(tmp_path),
        CodexLaunchSpec(
            model="gpt-test",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    assert phases == [
        StartupPhase.LAUNCHING_SUBPROCESS,
        StartupPhase.WAITING_FOR_CONNECTION,
        StartupPhase.INITIALIZING_SESSION,
        StartupPhase.HARNESS_READY,
    ]

    await connection._cleanup_resources(mark_stopped=False)


@pytest.mark.asyncio
async def test_codex_ws_primary_observer_dispatches_requests_to_handler() -> None:
    events: list[object] = []

    async def _event_sink(event: object) -> None:
        events.append(event)

    connection = codex_ws.CodexConnection(request_handler=InteractiveHandler(_event_sink))
    connection._primary_observer_mode = True

    await connection._handle_server_request(
        {
            "id": "observer-req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, codex_ws.HarnessEvent)
    assert event.event_type == "request/opened"
    assert event.payload["request_type"] == "approval"


@pytest.mark.asyncio
async def test_codex_ws_primary_observer_rejects_unsupported_server_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = codex_ws.CodexConnection()
    connection._primary_observer_mode = True
    captured_errors: list[tuple[object, int, str]] = []

    async def _fake_send_jsonrpc_error(
        request_id: object,
        *,
        code: int,
        message: str,
    ) -> None:
        captured_errors.append((request_id, code, message))

    monkeypatch.setattr(connection, "_send_jsonrpc_error", _fake_send_jsonrpc_error)

    await connection._handle_server_request(
        {
            "id": "observer-req-2",
            "method": "item/tool/unsupported",
            "params": {"threadId": "thread-1"},
        }
    )

    warning = await asyncio.wait_for(connection._event_queue.get(), timeout=1.0)
    assert warning is not None
    assert warning.event_type == "warning/unsupportedServerRequest"
    assert warning.payload == {
        "method": "item/tool/unsupported",
        "params": {"threadId": "thread-1"},
    }
    assert captured_errors == [
        (
            "observer-req-2",
            -32601,
            "Meridian codex_ws adapter does not support server request 'item/tool/unsupported'",
        )
    ]


@pytest.mark.asyncio
async def test_codex_ws_confirm_mode_rejects_only_when_handler_has_no_runtime_hitl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive_events: list[object] = []

    async def _interactive_event_sink(event: object) -> None:
        interactive_events.append(event)

    interactive_connection = codex_ws.CodexConnection(
        request_handler=InteractiveHandler(_interactive_event_sink)
    )
    interactive_connection._launch_spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(approval="confirm")
        )
    )
    interactive_errors: list[tuple[object, int, str]] = []

    async def _fake_send_jsonrpc_error_interactive(
        request_id: object,
        *,
        code: int,
        message: str,
    ) -> None:
        interactive_errors.append((request_id, code, message))

    monkeypatch.setattr(
        interactive_connection,
        "_send_jsonrpc_error",
        _fake_send_jsonrpc_error_interactive,
    )

    await interactive_connection._handle_server_request(
        {
            "id": "approval-req-interactive",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }
    )
    assert len(interactive_events) == 1
    interactive_event = interactive_events[0]
    assert isinstance(interactive_event, codex_ws.HarnessEvent)
    assert interactive_event.event_type == "request/opened"
    assert interactive_event.payload["request_type"] == "approval"
    assert interactive_errors == []

    auto_accept_connection = codex_ws.CodexConnection(request_handler=AutoAcceptHandler())
    auto_accept_connection._launch_spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(approval="confirm")
        )
    )
    captured_errors: list[tuple[object, int, str]] = []

    async def _fake_send_jsonrpc_error_auto_accept(
        request_id: object,
        *,
        code: int,
        message: str,
    ) -> None:
        captured_errors.append((request_id, code, message))

    monkeypatch.setattr(
        auto_accept_connection, "_send_jsonrpc_error", _fake_send_jsonrpc_error_auto_accept
    )

    await auto_accept_connection._handle_server_request(
        {
            "id": "approval-req-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        }
    )

    warning = await asyncio.wait_for(auto_accept_connection._event_queue.get(), timeout=1.0)
    assert warning is not None
    assert warning.event_type == "warning/approvalRejected"
    assert warning.payload == {
        "reason": "confirm_mode",
        "method": "item/commandExecution/requestApproval",
    }
    assert captured_errors == [
        (
            "approval-req-1",
            -32000,
            "Codex websocket approval requests are unsupported in confirm mode.",
        )
    ]


@pytest.mark.asyncio
async def test_codex_ws_managed_primary_uses_permission_broker(tmp_path: Path) -> None:
    connection = process_runner._create_managed_primary_connection(
        connection_factory=codex_ws.CodexConnection,
        harness_contract=get_default_harness_registry().get_contract(HarnessId.CODEX),
        spawn_dir=tmp_path / "spawn",
    )
    assert isinstance(connection, codex_ws.CodexConnection)
    assert isinstance(connection._request_handler, PermissionBroker)
    await connection._request_handler.handle_request(
        connection,
        HarnessRequest(
            request_id="approval-queue-test",
            request_type="approval",
            method="item/commandExecution/requestApproval",
            payload={"command": "echo"},
        ),
    )
    event = await asyncio.wait_for(connection.events().__anext__(), timeout=1.0)
    assert event is not None
    assert event.event_type == "request/opened"
    assert event.payload["request_id"] == "approval-queue-test"


def test_codex_ws_primary_runtime_request_policy_none_keeps_auto_accept() -> None:
    connection = codex_ws.CodexConnection()

    connection.configure_primary_runtime_requests(policy=PrimaryRuntimeRequestPolicy.NONE)

    assert isinstance(connection._request_handler, AutoAcceptHandler)


@pytest.mark.asyncio
async def test_codex_ws_server_request_resolved_clears_hitl() -> None:
    connection = codex_ws.CodexConnection()
    connection._hitl_requests = {"approval-1": 1, "approval-2": 2}

    await connection._handle_notification(
        method="serverRequest/resolved",
        payload={"requestId": "approval-1"},
        raw_text='{"method":"serverRequest/resolved"}',
    )

    assert connection._hitl_requests == {"approval-2": 2}


@pytest.mark.asyncio
async def test_codex_ws_respond_request_keeps_hitl_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = codex_ws.CodexConnection()
    connection._hitl_requests = {"approval-1": 1}

    async def _failing_send(_request_id: object, _result: dict[str, object]) -> None:
        raise RuntimeError("connection closed")

    monkeypatch.setattr(connection, "_send_jsonrpc_result", _failing_send)

    with pytest.raises(RuntimeError, match="connection closed"):
        await connection.respond_request("approval-1", "accept")

    assert connection._hitl_requests == {"approval-1": 1}


@pytest.mark.asyncio
async def test_codex_ws_respond_user_input_keeps_hitl_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = codex_ws.CodexConnection()
    connection._hitl_requests = {"input-1": 2}

    async def _failing_send(_request_id: object, _result: dict[str, object]) -> None:
        raise RuntimeError("connection closed")

    monkeypatch.setattr(connection, "_send_jsonrpc_result", _failing_send)

    with pytest.raises(RuntimeError, match="connection closed"):
        await connection.respond_user_input("input-1", {"name": "value"})

    assert connection._hitl_requests == {"input-1": 2}


@pytest.mark.asyncio
async def test_codex_ws_wait_for_rollout_materialization_waits_after_thread_start(
    tmp_path: Path,
) -> None:
    connection = codex_ws.CodexConnection()
    connection._state = "starting"
    connection._process = _FakeProcess()
    connection._ws = _FakeWebSocket()
    connection._thread_id = "11111111-1111-4111-8111-111111111111"
    connection._config = _build_connection_config(tmp_path)
    connection._codex_home = tmp_path / "codex-home"

    with pytest.raises(RuntimeError, match="rollout materialization"):
        await connection._wait_for_rollout_materialization(timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_codex_ws_wait_for_rollout_materialization_proceeds_before_turn_completed(
    tmp_path: Path,
) -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    codex_home = tmp_path / "codex-home"
    sessions_dir = codex_home / "sessions" / "2026" / "04" / "25"
    sessions_dir.mkdir(parents=True)

    connection = codex_ws.CodexConnection()
    connection._state = "starting"
    connection._process = _FakeProcess()
    connection._ws = _FakeWebSocket()
    connection._thread_id = session_id
    connection._current_turn_id = "turn-bootstrap"
    connection._config = _build_connection_config(tmp_path)
    connection._codex_home = codex_home

    async def _materialize_rollout() -> None:
        await asyncio.sleep(0.01)
        rollout_path = sessions_dir / f"rollout-2026-04-25T12-00-00-{session_id}.jsonl"
        rollout_path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": str(tmp_path),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    task = asyncio.create_task(_materialize_rollout())
    await connection._wait_for_rollout_materialization(timeout_seconds=0.5)
    await task
    assert connection._current_turn_id == "turn-bootstrap"


def test_codex_streaming_projection_builds_appserver_command_and_logs_ignored_report_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(sandbox="read-only", approval="auto")
        ),
        report_output_path="report.md",
        extra_args=("--invalid-flag",),
        projected_roots=(Path("/tmp/root-a"), Path("/tmp/root b")),
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_codex_streaming"
    ):
        command = project_codex_spec_to_appserver_command(
            spec,
            host="127.0.0.1",
            port=7777,
        )

    assert command[:4] == ["codex", "app-server", "--listen", "ws://127.0.0.1:7777"]
    assert _values_for_setting(command, "sandbox_mode") == ['"read-only"']
    assert _values_for_setting(command, "approval_policy") == ['"on-request"']
    assert _values_for_setting(command, "sandbox_workspace_write.writable_roots") == [
        '["/tmp/root-a", "/tmp/root b"]'
    ]
    assert command[-1:] == ["--invalid-flag"]
    assert (
        "Codex streaming ignores report_output_path; reports extracted from artifacts"
        in caplog.text
    )
    assert "Forwarding passthrough args to codex app-server: ['--invalid-flag']" in caplog.text


def test_codex_streaming_projection_default_approval_emits_no_policy_override(
    tmp_path: Path,
) -> None:
    spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(sandbox="workspace-write", approval="default")
        ),
    )

    command = project_codex_spec_to_appserver_command(
        spec,
        host="127.0.0.1",
        port=7778,
    )
    assert _values_for_setting(command, "approval_policy") == []
    assert _values_for_setting(command, "sandbox_mode") == ['"workspace-write"']

    method, payload = project_codex_spec_to_thread_request(spec, cwd=str(tmp_path))
    assert method == "thread/start"
    assert "approvalPolicy" not in payload
    assert payload["sandbox"] == "workspace-write"


def test_codex_streaming_projection_with_no_overrides_emits_clean_baseline_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(config=PermissionConfig())
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_codex_streaming"
    ):
        command = project_codex_spec_to_appserver_command(
            spec,
            host="127.0.0.1",
            port=7779,
        )

    assert command == ["codex", "app-server", "--listen", "ws://127.0.0.1:7779"]
    assert "Forwarding passthrough args to codex app-server" not in caplog.text
    assert "Codex streaming ignores report_output_path" not in caplog.text


def test_codex_streaming_projection_keeps_colliding_passthrough_config_args(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = CodexLaunchSpec(
        permission_resolver=TieredPermissionResolver(
            config=PermissionConfig(sandbox="read-only", approval="auto")
        ),
        extra_args=(
            "-c",
            'approval_policy="untrusted"',
            "-c",
            'sandbox_mode="workspace-write"',
        ),
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_codex_streaming"
    ):
        command = project_codex_spec_to_appserver_command(
            spec,
            host="127.0.0.1",
            port=7780,
        )

    assert _values_for_setting(command, "approval_policy") == ['"on-request"', '"untrusted"']
    assert _values_for_setting(command, "sandbox_mode") == ['"read-only"', '"workspace-write"']
    assert command[-4:] == [
        "-c",
        'approval_policy="untrusted"',
        "-c",
        'sandbox_mode="workspace-write"',
    ]
    assert (
        "Forwarding passthrough args to codex app-server: ['-c', "
        '\'approval_policy="untrusted"\', \'-c\', \'sandbox_mode="workspace-write"\']'
    ) in caplog.text


def test_codex_ws_thread_bootstrap_request_starts_new_thread(tmp_path: Path) -> None:
    method, payload = project_codex_spec_to_thread_request(
        CodexLaunchSpec(
            prompt="hello",
            model="gpt-5.3-codex",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        cwd=str(tmp_path),
    )

    assert method == "thread/start"
    assert payload == {"cwd": str(tmp_path), "model": "gpt-5.3-codex"}


def test_codex_ws_thread_bootstrap_request_projects_effort_and_permission_config(
    tmp_path: Path,
) -> None:
    method, payload = project_codex_spec_to_thread_request(
        CodexLaunchSpec(
            prompt="hello",
            model="gpt-5.3-codex",
            effort="high",
            permission_resolver=TieredPermissionResolver(
                config=PermissionConfig(sandbox="read-only", approval="auto")
            ),
        ),
        cwd=str(tmp_path),
    )

    assert method == "thread/start"
    assert payload == {
        "cwd": str(tmp_path),
        "model": "gpt-5.3-codex",
        "config": {"model_reasoning_effort": "high"},
        "approvalPolicy": "on-request",
        "sandbox": "read-only",
    }


def test_codex_ws_thread_bootstrap_request_resumes_existing_thread(tmp_path: Path) -> None:
    method, payload = project_codex_spec_to_thread_request(
        CodexLaunchSpec(
            prompt="hello",
            model="gpt-5.3-codex",
            continue_session_id="thread-123",
            permission_resolver=TieredPermissionResolver(
                config=PermissionConfig(approval="confirm")
            ),
        ),
        cwd=str(tmp_path),
    )

    assert method == "thread/resume"
    assert payload == {
        "cwd": str(tmp_path),
        "model": "gpt-5.3-codex",
        "approvalPolicy": "untrusted",
        "threadId": "thread-123",
    }


def test_codex_ws_thread_bootstrap_request_forks_existing_thread(tmp_path: Path) -> None:
    method, payload = project_codex_spec_to_thread_request(
        CodexLaunchSpec(
            prompt="hello",
            model="gpt-5.3-codex",
            continue_session_id="thread-123",
            continue_fork=True,
            permission_resolver=TieredPermissionResolver(
                config=PermissionConfig(sandbox="workspace-write", approval="default")
            ),
        ),
        cwd=str(tmp_path),
    )

    assert method == "thread/fork"
    assert payload == {
        "cwd": str(tmp_path),
        "model": "gpt-5.3-codex",
        "threadId": "thread-123",
        "sandbox": "workspace-write",
        "ephemeral": False,
    }


def test_codex_permission_mapping_fails_closed_on_unsupported_mode() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="approval mode 'unsupported'"):
        map_codex_approval_policy("unsupported")
