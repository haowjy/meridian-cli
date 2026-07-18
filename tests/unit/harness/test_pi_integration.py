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
from meridian.lib.harness.connections import pi_rpc as pi_rpc_module
from meridian.lib.harness.connections.base import ConnectionConfig, ConnectionNotReady, HarnessEvent
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.semantics import activity_transition, clears_signal, terminal_outcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.streaming.spawn_manager import SpawnManager

_PI_HELP_SURFACE = (
    "--mode rpc --model --append-system-prompt --session --fork "
    "--session-dir --no-extensions --no-skills "
    "--no-context-files --no-prompt-templates -e --extension "
    "PI_CODING_AGENT_SESSION_DIR"
)

def _is_pi_phase_event(event: HarnessEvent) -> bool:
    return event.event_type == "meridian.pi.lifecycle.phase"


async def _next_non_phase_event(event_iter):  # type: ignore[no-untyped-def]
    while True:
        event = await anext(event_iter)
        if not _is_pi_phase_event(event):
            return event


def _configure_extension_projection(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    source_root = root / "dist" / "extensions"
    for extension_name in ("managed-bash", "meridian-spawn-watch"):
        (source_root / extension_name).mkdir(parents=True, exist_ok=True)
        (source_root / extension_name / "index.js").write_text(
            "export default {}\\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(root / "agent" / "extensions"))


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


async def _start_existing_pi_connection(
    connection: PiRpcConnection,
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> None:
    resolve_spawn_log_dir(config.control_root, config.spawn_id).mkdir(
        parents=True,
        exist_ok=True,
    )
    await connection.start(config, spec)


def _history_events(runtime_root: Path, spawn_id: SpawnId) -> list[dict[str, object]]:
    history_path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


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


@pytest.mark.parametrize(
    ("line", "expected_reason"),
    [
        ("{bad json", "malformed_json"),
    ],
)
def test_pi_rpc_connection_surfaces_stdout_parse_diagnostics(
    line: str,
    expected_reason: str,
) -> None:
    event = PiRpcConnection()._parse_stdout_line(line)

    assert event is not None
    assert event.event_type == "meridian.lifecycle.parse_error"
    assert event.payload["reason"] == expected_reason
    assert event.harness_id == HarnessId.PI.value



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

    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-rpc\"}'\n"
        "while IFS= read -r line; do\n"
        "  printf '%s\\n' \"$line\" >> \"$PI_RPC_INBOUND_LOG\"\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      case \"$line\" in\n"
        "        *'\"message\":\"FIRST\"'*) printf '%s\\n' "
        "'{\"id\":\"meridian-prompt-1\",\"type\":\"response\","
        "\"command\":\"prompt\",\"success\":true}' ;;\n"
        "        *'\"message\":\"SECOND\"'*) printf '%s\\n' "
        "'{\"id\":\"meridian-prompt-2\",\"type\":\"response\","
        "\"command\":\"prompt\",\"success\":true}' ;;\n"
        "      esac\n"
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
    await _start_existing_pi_connection(
        connection,
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
    first = await _next_non_phase_event(event_iter)
    assert first.event_type == "session"
    assert connection.session_id == "ses-rpc"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"

    first_send = asyncio.create_task(connection.send_user_message("FIRST"))
    assert (await _next_non_phase_event(event_iter)).event_type == "response"
    await first_send
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"

    second_send = asyncio.create_task(connection.send_user_message("SECOND"))
    assert (await _next_non_phase_event(event_iter)).event_type == "response"
    await second_send
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"

    await connection.send_steer("focus")
    assert (await _next_non_phase_event(event_iter)).event_type == "message_update"

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
async def test_pi_rpc_connection_launches_resolved_runtime_with_scoped_session_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    fake_pi = tmp_path / "bin" / "pi-fake"
    fake_pi.parent.mkdir(parents=True, exist_ok=True)
    observed_path = tmp_path / "pi-runtime-observed.json"
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
        "    print('pi 3.0.0')\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--help':\n"
        f"    print({json.dumps(_PI_HELP_SURFACE)})\n"
        "    raise SystemExit(0)\n"
        "observed_path = os.environ.get('PI_RPC_OBSERVED_PATH', '').strip()\n"
        "if observed_path:\n"
        "    with open(observed_path, 'w', encoding='utf-8') as handle:\n"
        "        json.dump({'argv': sys.argv}, handle)\n"
        "for line in sys.stdin:\n"
        "    payload = json.loads(line)\n"
        "    payload_type = payload.get('type')\n"
        "    if payload_type == 'prompt':\n"
        "        print(json.dumps({'type': 'agent_start'}), flush=True)\n"
        "        print(\n"
        "            json.dumps(\n"
        "                {\n"
        "                    'type': 'agent_end',\n"
        "                    'messages': [{'role': 'assistant', 'stopReason': 'stop'}],\n"
        "                }\n"
        "            ),\n"
        "            flush=True,\n"
        "        )\n"
        "        continue\n"
        "    if payload_type == 'abort':\n"
        "        raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    scoped_session_dir = tmp_path / "pi-sessions" / "p-pi-direct-runtime"
    connection = PiRpcConnection()
    await _start_existing_pi_connection(
        connection,
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-direct-runtime"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={
                "MERIDIAN_PI_BINARY": str(fake_pi),
                "PI_CODING_AGENT_SESSION_DIR": str(scoped_session_dir),
                "PI_RPC_OBSERVED_PATH": str(observed_path),
            },
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    event_iter = connection.events()
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"
    await connection.send_cancel()
    _ = [event async for event in event_iter]

    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    argv = observed["argv"]
    assert argv[0] == str(fake_pi)
    assert argv[argv.index("--mode") + 1] == "rpc"
    assert argv[argv.index("--session-dir") + 1] == str(scoped_session_dir)

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_redacts_secret_like_cli_args_in_process_spawned_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-redacted\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
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
            spawn_id=SpawnId("p-pi-redacted-argv"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={
                "PI_CODING_AGENT_SESSION_DIR": str(scoped_session_dir),
            },
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            extra_args=("--api-key", "secret-value", "--profile", "safe"),
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
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


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_ignores_non_lifecycle_stderr_lines_but_logs_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-stderr-ignore")
    plain_stderr = "warning from stderr"
    non_allowlisted_json = '{"type":"pi.runtime.warn","message":"ignore-me"}'
    lifecycle_stderr = (
        '{"type":"meridian.quiescence.ready","schema_version":1,'
        '"parent_spawn_id":"p-pi-stderr-ignore","correlation_id":"q-stderr",'
        '"emitted_at_ms":1760000000000}'
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-stderr-ignore\"}'\n"
        f"printf '%s\\n' '{plain_stderr}' >&2\n"
        f"printf '%s\\n' '{non_allowlisted_json}' >&2\n"
        f"printf '%s\\n' '{lifecycle_stderr}' >&2\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"agent_start\"}'\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    connection = PiRpcConnection()
    await _start_existing_pi_connection(
        connection,
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

    event_iter = connection.events()
    non_phase_events: list[HarnessEvent] = []
    while True:
        event = await _next_non_phase_event(event_iter)
        non_phase_events.append(event)
        if event.event_type == "agent_end":
            break
    await connection.send_cancel()
    non_phase_events.extend(
        [event async for event in event_iter if not _is_pi_phase_event(event)]
    )

    assert not any(event.event_type == "pi.runtime.warn" for event in non_phase_events)
    assert not any(event.event_type == "meridian.quiescence.ready" for event in non_phase_events)
    assert not any(
        event.event_type == "meridian.lifecycle.parse_error"
        for event in non_phase_events
    )
    stderr_log = resolve_spawn_log_dir(tmp_path, spawn_id) / "stderr.log"
    stderr_text = stderr_log.read_text(encoding="utf-8")
    assert plain_stderr in stderr_text
    assert non_allowlisted_json in stderr_text
    assert lifecycle_stderr in stderr_text


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
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
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

    spawn_id = SpawnId("p-pi-autoprompt-quiesce")
    _publish_manager_spawn(tmp_path, spawn_id)
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_pi_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
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
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome.status == "succeeded"
        assert outcome.exit_code == 0
        assert outcome.error is None

        inbound_messages = [
            json.loads(line)
            for line in inbound_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [message["type"] for message in inbound_messages] == ["prompt"]
        assert inbound_messages[0]["message"] == "hello auto prompt"
    finally:
        await manager.shutdown()


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
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_KILL_GRACE_SECONDS", 0.01)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    if scenario == "no_session":
        prompt_handler = (
            "      printf '%s\n' '{\"type\":\"agent_start\"}'\n"
            "      printf '%s\n' "
            "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        )
    else:
        prompt_handler = "      sleep 30\n"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        f"{prompt_handler}"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
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
        start_connection=_start_pi_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: _NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello startup",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello startup",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome.status == expected_status
        assert outcome.error == expected_error
        assert any(
            event.get("event_type") == "meridian.pi.lifecycle.phase"
            and event.get("payload", {}).get("phase") == diagnostic_marker
            for event in _history_events(tmp_path, spawn_id)
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_prompt_response_failure_fails_fast_with_reported_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' "
        "'{\"type\":\"response\",\"command\":\"prompt\",\"success\":false,"
        "\"error\":\"No API key configured\"}'\n"
        "      sleep 1\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
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
        outcome = await manager.wait_for_completion(spawn_id)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "No API key configured"

        response_events = [
            event
            for event in _history_events(tmp_path, spawn_id)
            if event.get("event_type") == "response"
            and event.get("payload", {}).get("success") is False
        ]
        assert len(response_events) == 1
        assert response_events[0]["payload"]["error"] == "No API key configured"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_connection_launches_in_control_root_when_task_cwd_provided(
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
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "pwd > \"$PI_TEST_CWD_FILE\"\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-task-cwd\"}'\n"
        # Stay alive long enough for the connection to write the initial prompt,
        # then read one line (the prompt) before exiting cleanly.
        "read _prompt_line || true\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_TEST_CWD_FILE", str(observed_cwd))

    connection = PiRpcConnection()
    await _start_existing_pi_connection(
        connection,
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
    assert (await _next_non_phase_event(event_iter)).event_type == "session"
    await connection.stop(reason="test")
    _ = [event async for event in event_iter]

    assert observed_cwd.read_text(encoding="utf-8").strip() == str(control_root)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_surfaces_stderr_on_early_exit_before_first_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_KILL_GRACE_SECONDS", 0.01)

    spawn_id = SpawnId("p-pi-stderr-early-exit")
    crash_stderr = "TypeError: markAsUncloneable is not a function"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "# RPC mode: accept the initial prompt line, then die before any stdout event.\n"
        "read -r _prompt_line || true\n"
        f"printf '%s\\n' '{crash_stderr}' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    connection = PiRpcConnection()
    await _start_existing_pi_connection(
        connection,
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

    events = [event async for event in connection.events()]
    error_events = [
        event
        for event in events
        if event.event_type == "error/connectionClosed"
    ]
    assert error_events
    message = str(error_events[0].payload.get("message", ""))
    assert crash_stderr in message
    assert "Pi subprocess stderr:" in message

    stderr_log = resolve_spawn_log_dir(tmp_path, spawn_id) / "stderr.log"
    assert crash_stderr in stderr_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pi_rpc_connection_start_fails_fast_when_runtime_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    expected_error = "runtime probe failed before launch"

    def _fail_resolve_runtime(*, env: dict[str, str], role: str) -> object:
        _ = env, role
        raise pi_rpc_module.PiRuntimeResolutionError(expected_error)

    monkeypatch.setattr(pi_rpc_module, "resolve_pi_runtime", _fail_resolve_runtime)

    connection = PiRpcConnection()
    with pytest.raises(ConnectionNotReady, match=rf"^{expected_error}$"):
        await _start_existing_pi_connection(
            connection,
            ConnectionConfig(
                spawn_id=SpawnId("p-pi-runtime-resolution-error"),
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

    assert connection.state == "failed"
    assert connection.subprocess_pid is None
    assert connection.session_id is None
