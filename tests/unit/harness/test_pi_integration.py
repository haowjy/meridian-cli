# qa-validated: pi-rpc-quiescence
"""Pi harness wiring tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import BootstrapMode, ForkMaterializationMode, SpawnParams
from meridian.lib.harness.connections import pi_rpc as pi_rpc_module
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.harness.semantics import activity_transition, clears_signal, terminal_outcome
from meridian.lib.launch.env import build_harness_env_overrides
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.safety.permissions import PermissionConfig, UnsafeNoOpPermissionResolver
from meridian.lib.streaming.spawn_manager import SpawnManager


def _configure_extension_projection(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    source_root = root / "dist" / "extensions"
    (source_root / "managed-bash").mkdir(parents=True, exist_ok=True)
    (source_root / "managed-bash" / "index.js").write_text("export default {}\\n", encoding="utf-8")
    (source_root / "meridian-lifecycle").mkdir(parents=True, exist_ok=True)
    (source_root / "meridian-lifecycle" / "index.js").write_text(
        "export default {}\\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(root / "agent" / "extensions"))


def test_pi_adapter_registered_with_expected_rpc_contract() -> None:
    registry = HarnessRegistry.with_defaults()
    contract = registry.get_contract(HarnessId.PI)

    assert contract.bootstrap.mode is BootstrapMode.SUBPROCESS_ONLY
    assert contract.bootstrap.fork_materialization is ForkMaterializationMode.NATIVE_CONTINUE_FORK
    assert contract.capabilities.supports_session_fork is True
    assert contract.capabilities.terminal_surface_modes == (TerminalSurfaceMode.PURE_STDIO,)

    adapter = registry.get_subprocess_harness(HarnessId.PI)
    overrides = adapter.env_overrides(
        PermissionConfig(
            pi_launch_config_path="/tmp/pi-launch-config.json",
        )
    )
    assert Path(overrides["PI_CODING_AGENT_DIR"]).parts[-2:] == ("meridian-pi", "agent")
    assert overrides["MERIDIAN_PI_LAUNCH_CONFIG"] == "/tmp/pi-launch-config.json"


@pytest.mark.parametrize(
    ("interactive", "expected_role"),
    [
        (False, "spawned"),
        (True, "primary"),
    ],
)
def test_pi_launch_env_injects_session_role(interactive: bool, expected_role: str) -> None:
    registry = HarnessRegistry.with_defaults()
    adapter = registry.get_subprocess_harness(HarnessId.PI)

    env = build_harness_env_overrides(
        adapter=adapter,
        run_params=SpawnParams(prompt="hello", interactive=interactive),
        permission_config=PermissionConfig(),
    )

    assert env["MERIDIAN_PI_SESSION_ROLE"] == expected_role


def test_pi_adapter_build_command_uses_rpc_mode_for_spawned_and_primary_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    registry = HarnessRegistry.with_defaults()
    adapter = registry.get_subprocess_harness(HarnessId.PI)
    perms = UnsafeNoOpPermissionResolver(_suppress_warning=True)

    spawned_command = adapter.build_command(
        SpawnParams(prompt="hello from spawn", interactive=False),
        perms,
    )
    assert "--mode" in spawned_command
    assert spawned_command[spawned_command.index("--mode") + 1] == "rpc"

    primary_command = adapter.build_command(
        SpawnParams(prompt="hello from primary", interactive=True),
        perms,
    )
    assert "--mode" in primary_command
    assert primary_command[primary_command.index("--mode") + 1] == "rpc"

    primary_env = build_harness_env_overrides(
        adapter=adapter,
        run_params=SpawnParams(prompt="hello from primary", interactive=True),
        permission_config=PermissionConfig(),
    )
    assert primary_env["MERIDIAN_PI_SESSION_ROLE"] == "primary"


@pytest.mark.parametrize(
    ("line", "expected_reason"),
    [
        ("{bad json", "malformed_json"),
        ("[]", "non_object"),
        ('{"id":"missing-type"}', "missing_type"),
    ],
)
def test_pi_rpc_connection_surfaces_parse_diagnostics(
    line: str,
    expected_reason: str,
) -> None:
    event = PiRpcConnection()._parse_stdout_line(line)
    assert event is not None
    assert event.event_type == "meridian.lifecycle.parse_error"
    assert event.payload["type"] == "meridian.lifecycle.parse_error"
    assert event.payload["reason"] == expected_reason
    assert event.harness_id == HarnessId.PI.value


def test_pi_rpc_connection_rejects_unsupported_canonical_lifecycle_schema_version() -> None:
    event = PiRpcConnection()._parse_stdout_line(
        '{"type":"meridian.subspawn.start","schema_version":2,"subspawn_id":"j-1"}'
    )
    assert event is not None
    assert event.event_type == "meridian.lifecycle.parse_error"
    assert event.payload["type"] == "meridian.lifecycle.parse_error"
    assert event.payload["schema_version"] == 1
    assert event.payload["error"] == "unsupported_schema_version"
    assert event.payload["raw_type"] == "meridian.subspawn.start"
    assert event.payload["reason"] == "unsupported_schema_version"


def test_pi_rpc_connection_allows_non_lifecycle_schema_version_passthrough() -> None:
    event = PiRpcConnection()._parse_stdout_line(
        '{"type":"message_update","schema_version":2,"content":"still-valid"}'
    )
    assert event is not None
    assert event.event_type == "message_update"
    assert event.payload["schema_version"] == 2


def test_pi_semantics_terminal_outcome_and_activity_mapping() -> None:
    success_event = HarnessEvent(
        event_type="agent_end",
        harness_id="pi",
        payload={
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "stop",
                }
            ]
        },
    )
    error_event = HarnessEvent(
        event_type="agent_end",
        harness_id="pi",
        payload={
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "error",
                }
            ]
        },
    )
    cancelled_event = HarnessEvent(
        event_type="agent_end",
        harness_id="pi",
        payload={
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "cancelled",
                }
            ]
        },
    )

    assert terminal_outcome(success_event) is not None
    assert terminal_outcome(success_event).status == "succeeded"
    assert terminal_outcome(error_event) is not None
    assert terminal_outcome(error_event).status == "failed"
    assert terminal_outcome(cancelled_event) is not None
    assert terminal_outcome(cancelled_event).status == "cancelled"
    assert terminal_outcome(cancelled_event).error == "cancelled"
    assert activity_transition(
        HarnessEvent(event_type="message_update", harness_id="pi", payload={})
    ) == "turn_active"
    assert (
        activity_transition(HarnessEvent(event_type="agent_end", harness_id="pi", payload={}))
        == "idle"
    )
    assert clears_signal(HarnessEvent(event_type="agent_end", harness_id="pi", payload={})) is True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_supports_multi_turn_injection_and_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    inbound_log = tmp_path / "pi-inbound.jsonl"

    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-rpc\"}'\n"
        "while IFS= read -r line; do\n"
        "  printf '%s\\n' \"$line\" >> \"$PI_RPC_INBOUND_LOG\"\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"agent_start\"}'\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      ;;\n"
        "    *'\"type\":\"steer\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"message_update\"}'\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*)\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_RPC_INBOUND_LOG", str(inbound_log))

    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-rpc-connection"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    event_iter = connection.events()
    first = await anext(event_iter)
    assert first.event_type == "session"
    assert connection.session_id == "ses-rpc"
    assert (await anext(event_iter)).event_type == "agent_start"
    assert (await anext(event_iter)).event_type == "agent_end"

    await connection.send_user_message("FIRST")
    assert (await anext(event_iter)).event_type == "agent_start"
    assert (await anext(event_iter)).event_type == "agent_end"

    await connection.send_user_message("SECOND")
    assert (await anext(event_iter)).event_type == "agent_start"
    assert (await anext(event_iter)).event_type == "agent_end"

    await connection.send_steer("focus")
    assert (await anext(event_iter)).event_type == "message_update"

    await connection.send_cancel()
    remaining_events = [event async for event in event_iter]
    assert remaining_events == []

    inbound_messages = [
        json.loads(line) for line in inbound_log.read_text(encoding="utf-8").splitlines() if line
    ]
    assert inbound_messages[0]["type"] == "prompt"
    assert inbound_messages[0]["message"] == "hello"
    assert [message["type"] for message in inbound_messages] == [
        "prompt",
        "prompt",
        "prompt",
        "steer",
        "abort",
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_stop_timeout_terminates_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_KILL_GRACE_SECONDS", 0.01)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    inbound_log = tmp_path / "pi-inbound.jsonl"

    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-stop-timeout\"}'\n"
        "while IFS= read -r line; do\n"
        "  printf '%s\\n' \"$line\" >> \"$PI_RPC_INBOUND_LOG\"\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"abort\"'*) sleep 30 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_RPC_INBOUND_LOG", str(inbound_log))

    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-stop-timeout"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    event_iter = connection.events()
    assert (await anext(event_iter)).event_type == "session"

    await connection.stop(reason="quiescent")

    inbound_messages = [
        json.loads(line) for line in inbound_log.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [message["type"] for message in inbound_messages] == ["prompt", "abort"]
    assert inbound_messages[0]["message"] == "hello"
    assert connection.state == "stopped"
    assert connection.subprocess_pid is None
    with pytest.raises(StopAsyncIteration):
        await anext(event_iter)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_malformed_canonical_event_fails_closed_through_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-malformed\"}'\n"
        "printf '%s\\n' '{\"type\":\"message_update\",\"delta\":\"before malformed\"}'\n"
        "printf '%s\\n' '{\"type\":\"meridian.subspawn.start\",\"schema_version\":2}'\n"
        "printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    class NoopControlServer:
        endpoint = None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> PiRpcConnection:
        connection = PiRpcConnection()
        await connection.start(config, spec)
        return connection

    spawn_id = SpawnId("p-pi-live-malformed")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert (
            outcome.error
            == "pi_lifecycle_tracking_invalidated:"
            "unsupported_schema_event:meridian.subspawn.start"
        )
        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert any(event["event_type"] == "message_update" for event in history)
        parse_errors = [
            event
            for event in history
            if event["event_type"] == "meridian.lifecycle.parse_error"
        ]
        assert len(parse_errors) == 1
        assert parse_errors[0]["payload"]["raw_type"] == "meridian.subspawn.start"
        assert parse_errors[0]["payload"]["raw_line"] == (
            '{"type":"meridian.subspawn.start","schema_version":2}'
        )
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_auto_delivers_initial_prompt_and_quiesces_without_inject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    inbound_log = tmp_path / "pi-inbound.jsonl"
    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-autoprompt\"}'\n"
        "while IFS= read -r line; do\n"
        "  printf '%s\\n' \"$line\" >> \"$PI_RPC_INBOUND_LOG\"\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"agent_start\"}'\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*)\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_RPC_INBOUND_LOG", str(inbound_log))

    class NoopControlServer:
        endpoint = None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def _start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> PiRpcConnection:
        connection = PiRpcConnection()
        await connection.start(config, spec)
        return connection

    spawn_id = SpawnId("p-pi-autoprompt-quiesce")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        pi_quiescence_idle_grace_secs=0.01,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello auto prompt",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello auto prompt",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        assert outcome.status == "succeeded"
        assert outcome.exit_code == 0
        assert outcome.error is None

        inbound_messages = [
            json.loads(line)
            for line in inbound_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [message["type"] for message in inbound_messages] == ["prompt", "abort"]
        assert inbound_messages[0]["message"] == "hello auto prompt"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_connection_launches_in_task_cwd_when_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    observed_cwd = tmp_path / "observed-cwd.txt"
    task_cwd = tmp_path / "task"
    task_cwd.mkdir()
    control_root = tmp_path / "control"
    control_root.mkdir()
    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "pwd > \"$PI_TEST_CWD_FILE\"\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-task-cwd\"}'\n"
        "printf '%s\\n' '{\"type\":\"abort\"}' > /dev/null\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_TEST_CWD_FILE", str(observed_cwd))

    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-task-cwd"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=control_root,
            env_overrides={},
            task_cwd=task_cwd,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    event_iter = connection.events()
    assert (await anext(event_iter)).event_type == "session"
    await connection.stop(reason="test")
    _ = [event async for event in event_iter]

    assert observed_cwd.read_text(encoding="utf-8").strip() == str(task_cwd)
