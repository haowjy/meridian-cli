# qa-validated: pi-rpc-quiescence
"""Pi harness wiring tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeIdentity
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections import pi_rpc as pi_rpc_module
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    RawHarnessEvent,
)
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection, PiRpcTimingPolicy
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import (
    LaunchPaths,
    resolve_project_runtime_root_for_write,
)
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.launch_paths import launch_paths
from tests.support.pi_extensions import configure_pi_extension_projection

_PI_HELP_SURFACE = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions -e --extension "
    "PI_CODING_AGENT_SESSION_DIR"
)


def _is_pi_phase_event(event: RawHarnessEvent) -> bool:
    return event.event_type == "meridian.pi.lifecycle.phase"


async def _start_existing_pi_connection(
    connection: PiRpcConnection,
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> None:
    launch = config.launch
    if spawn_store.get_spawn(launch.runtime_root, launch.spawn_id) is None:
        _publish_manager_spawn(launch.runtime_root, launch.spawn_id)
    await connection.start(config, spec)


def _launch(project_root: Path, spawn_id: SpawnId) -> LaunchPaths:
    return launch_paths(resolve_project_runtime_root_for_write(project_root), spawn_id)


def _publish_manager_spawn(runtime_root: Path, spawn_id: SpawnId) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=str(spawn_id),
        model="test-model",
        agent="test-agent",
        harness="pi",
        prompt="hello",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_redacts_secret_like_cli_args_in_process_spawned_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'pi 1.2.3\'; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo \'{_PI_HELP_SURFACE}\'; exit 0; fi\n'
        'printf \'%s\\n\' \'{"type":"session","id":"ses-redacted"}\'\n'
        "while IFS= read -r line; do\n"
        '  case "$line" in\n'
        '    *\'"type":"abort"\'*) exit 0 ;;\n'
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    connection = PiRpcConnection()
    scoped_session_dir = tmp_path / "sessions" / "p-pi-redacted-argv"
    await _start_existing_pi_connection(
        connection,
        ConnectionConfig(
            launch=_launch(tmp_path, SpawnId("p-pi-redacted-argv")),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            child_env={
                "PI_CODING_AGENT_SESSION_DIR": str(scoped_session_dir),
            },
        ),
        ResolvedLaunchSpec(
            native_identity=NativeIdentity(
                "pi",
                "create",
                str(scoped_session_dir),
                "planned-id",
                None,
                None,
            ),
            harness=HarnessId.PI,
            prompt="hello",
            extra_args=("--api-key", "secret-value", "--profile", "safe"),
        ),
    )

    event_iter = connection.events()
    first = await anext(event_iter)
    assert first.event_type == "meridian.pi.lifecycle.phase"
    assert first.payload.get("phase") == "process_spawned"
    command = first.payload.get("command")
    assert isinstance(command, list)
    assert command[command.index("--session-dir") + 1] == str(scoped_session_dir)
    assert "--api-key" in command
    assert "secret-value" not in command
    assert "<redacted>" in command

    await connection.stop(reason="test")
    _ = [event async for event in event_iter]


class _NoopControlServer:
    endpoint = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _start_pi_connection(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> PiRpcConnection:
    connection = PiRpcConnection()
    await _start_existing_pi_connection(connection, config, spec)
    return connection


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_error", "diagnostic_marker"),
    [
        pytest.param(
            "no_session",
            "succeeded",
            None,
            "session_event_absent",
            id="no-session",
        ),
        pytest.param(
            "first_event_timeout",
            "failed",
            "pi_rpc_no_response_after_initial_prompt",
            "first_pi_event_timeout",
            id="first-event-timeout",
        ),
    ],
)
async def test_pi_spawn_manager_startup_diagnostics_report_outcome_and_marker(
    scenario: str,
    expected_status: str,
    expected_error: str | None,
    diagnostic_marker: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)

    async def start_connection(
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> PiRpcConnection:
        connection = PiRpcConnection(timing=PiRpcTimingPolicy(first_event_timeout_seconds=2.0))
        await _start_existing_pi_connection(connection, config, spec)
        return connection

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    if scenario == "no_session":
        prompt_handler = (
            "      printf '%s\n' '{\"type\":\"agent_start\"}'\n"
            "      printf '%s\n' "
            '\'{"type":"agent_end","messages":[{"role":"assistant","stopReason":"stop"}]}\'\n'
            '      printf \'%s\\n\' \'{"type":"agent_settled","aborted":false}\'\n'
        )
    else:
        prompt_handler = "      sleep 30\n"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'pi 1.2.3\'; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo \'{_PI_HELP_SURFACE}\'; exit 0; fi\n'
        "while IFS= read -r line; do\n"
        '  case "$line" in\n'
        '    *\'"type":"prompt"\'*)\n'
        f"{prompt_handler}"
        "      ;;\n"
        '    *\'"type":"abort"\'*) exit 0 ;;\n'
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    spawn_id = SpawnId(f"p-pi-{scenario}")
    _publish_manager_spawn(tmp_path, spawn_id)
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    observed: list[RawHarnessEvent] = []
    manager.register_event_hook(spawn_id, observed.append)
    await manager.start_spawn(
        ConnectionConfig(
            launch=_launch(tmp_path, spawn_id),
            harness_id=HarnessId.PI,
            prompt="hello startup",
            control_root=tmp_path,
            child_env={"PATH": os.environ["PATH"]},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello startup",
        ),
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome.status == expected_status
        assert outcome.error == expected_error
        assert any(
            event.event_type == "meridian.pi.lifecycle.phase"
            and event.payload.get("phase") == diagnostic_marker
            for event in observed
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_prompt_response_failure_fails_fast_with_reported_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'pi 1.2.3\'; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo \'{_PI_HELP_SURFACE}\'; exit 0; fi\n'
        "while IFS= read -r line; do\n"
        '  case "$line" in\n'
        '    *\'"type":"prompt"\'*)\n'
        "      printf '%s\\n' "
        '\'{"type":"response","command":"prompt","success":false,'
        '"error":"No API key configured"}\'\n'
        "      sleep 1\n"
        "      printf '%s\\n' "
        '\'{"type":"agent_end","messages":[{"role":"assistant","stopReason":"stop"}]}\'\n'
        "      ;;\n"
        '    *\'"type":"abort"\'*) exit 0 ;;\n'
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    spawn_id = SpawnId("p-pi-prompt-failed-response")
    _publish_manager_spawn(tmp_path, spawn_id)
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_pi_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    observed: list[RawHarnessEvent] = []
    manager.register_event_hook(spawn_id, observed.append)
    await manager.start_spawn(
        ConnectionConfig(
            launch=_launch(tmp_path, spawn_id),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            child_env={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
        ),
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "No API key configured"

        response_events = [
            event
            for event in observed
            if event.event_type == "response" and event.payload.get("success") is False
        ]
        assert len(response_events) == 1
        assert response_events[0].payload["error"] == "No API key configured"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_malformed_line_does_not_satisfy_first_event_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo \'pi 1.2.3\'; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo \'{_PI_HELP_SURFACE}\'; exit 0; fi\n'
        "read -r _prompt_line || true\n"
        "printf '%s\\n' '{bad json'\n"
        "sleep 5\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    connection = PiRpcConnection(timing=PiRpcTimingPolicy(first_event_timeout_seconds=1.0))
    await _start_existing_pi_connection(
        connection,
        ConnectionConfig(
            launch=_launch(tmp_path, SpawnId("p-pi-malformed-first-event")),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            child_env={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
        ),
    )

    events = [event async for event in connection.events()]

    assert any(event.event_type == "meridian.lifecycle.parse_error" for event in events)
    error_messages = [
        str(event.payload.get("message", ""))
        for event in events
        if event.event_type == "meridian/error/connectionClosed"
    ]
    assert error_messages
    assert pi_rpc_module._PI_FIRST_EVENT_TIMEOUT_REASON in error_messages[0]
