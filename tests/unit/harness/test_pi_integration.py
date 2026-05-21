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
from meridian.lib.harness.connections.pi_lifecycle_file import PiLifecycleEventTailer
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.semantics import activity_transition, clears_signal, terminal_outcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.streaming.spawn_manager import SpawnManager

_PI_HELP_SURFACE = (
    "--mode rpc --model --append-system-prompt --session --fork "
    "--session-dir --no-extensions --no-skills "
    "--no-context-files --no-prompt-templates -e --extension "
    "PI_CODING_AGENT_SESSION_DIR"
)
_PI_LIFECYCLE_EVENT_FILE_ENV = "MERIDIAN_PI_LIFECYCLE_EVENT_FILE"
_PI_LIFECYCLE_EVENT_FILENAME = "pi-lifecycle-events.jsonl"


def _is_pi_phase_event(event: HarnessEvent) -> bool:
    return event.event_type == "meridian.pi.lifecycle.phase"


async def _next_non_phase_event(event_iter):  # type: ignore[no-untyped-def]
    while True:
        event = await anext(event_iter)
        if not _is_pi_phase_event(event):
            return event


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


def _lifecycle_sidecar_path(control_root: Path, spawn_id: SpawnId) -> Path:
    return resolve_spawn_log_dir(control_root, spawn_id) / _PI_LIFECYCLE_EVENT_FILENAME


def test_pi_lifecycle_event_tailer_ignores_truncated_final_line(tmp_path: Path) -> None:
    spawn_id = SpawnId("p-pi-lifecycle-truncated-tailer")
    lifecycle_path = tmp_path / "pi-lifecycle-events.jsonl"
    complete_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-lifecycle-truncated-tailer","correlation_id":"j-ok",'
        '"subspawn_id":"j-ok","emitted_at_ms":1760000000000}'
    )
    truncated_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-lifecycle-truncated-tailer","correlation_id":"j-truncated"'
    )
    lifecycle_path.write_text(f"{complete_line}\n{truncated_line}", encoding="utf-8")

    tailer = PiLifecycleEventTailer(
        file_path=lifecycle_path,
        spawn_id=spawn_id,
        harness_id=HarnessId.PI.value,
    )
    tailer.open()
    try:
        events = tailer.catch_up_to_eof()
    finally:
        tailer.close()

    assert [event.event_type for event in events] == ["meridian.subspawn.start"]
    assert events[0].payload["subspawn_id"] == "j-ok"


def test_pi_lifecycle_event_tailer_preserves_partial_line_until_newline(tmp_path: Path) -> None:
    spawn_id = SpawnId("p-pi-lifecycle-split-tailer")
    lifecycle_path = tmp_path / "pi-lifecycle-events.jsonl"
    lifecycle_path.touch()
    event_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-lifecycle-split-tailer","correlation_id":"j-split",'
        '"subspawn_id":"j-split","emitted_at_ms":1760000000000}'
    )
    split_at = event_line.index('"correlation_id"')

    tailer = PiLifecycleEventTailer(
        file_path=lifecycle_path,
        spawn_id=spawn_id,
        harness_id=HarnessId.PI.value,
    )
    tailer.open()
    try:
        with lifecycle_path.open("ab") as handle:
            handle.write(event_line[:split_at].encode("utf-8"))
            handle.flush()
        assert tailer.read_ready_events() == []

        with lifecycle_path.open("ab") as handle:
            handle.write(event_line[split_at:].encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
        events = tailer.read_ready_events()
    finally:
        tailer.close()

    assert [event.event_type for event in events] == ["meridian.subspawn.start"]
    assert events[0].payload["subspawn_id"] == "j-split"


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
    first = await _next_non_phase_event(event_iter)
    assert first.event_type == "session"
    assert connection.session_id == "ses-rpc"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"

    await connection.send_user_message("FIRST")
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_start"
    assert (await _next_non_phase_event(event_iter)).event_type == "agent_end"

    await connection.send_user_message("SECOND")
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
async def test_pi_rpc_connection_launches_resolved_runtime_with_scoped_session_dir_and_managed_extensions(  # noqa: E501
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
        "    env_keys = (\n"
        "        'MERIDIAN_PI_BINARY',\n"
        "        'MERIDIAN_PI_SESSION_ROLE',\n"
        "        'MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS',\n"
        "        'MERIDIAN_PI_LIFECYCLE_EVENT_FILE',\n"
        "        'PI_CODING_AGENT_SESSION_DIR',\n"
        "        'PI_CODING_AGENT_DIR',\n"
        "    )\n"
        "    observed_env = {key: os.environ[key] for key in env_keys if key in os.environ}\n"
        "    with open(observed_path, 'w', encoding='utf-8') as handle:\n"
        "        json.dump({'argv': sys.argv, 'env': observed_env}, handle)\n"
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
    managed_entrypoints = (
        str(tmp_path / "agent" / "extensions" / "managed-bash" / "index.js"),
        str(tmp_path / "agent" / "extensions" / "meridian-lifecycle" / "index.js"),
    )

    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-direct-runtime"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={
                "MERIDIAN_PI_BINARY": str(fake_pi),
                "MERIDIAN_PI_SESSION_ROLE": "spawned",
                "PI_CODING_AGENT_SESSION_DIR": str(scoped_session_dir),
                "PI_RPC_OBSERVED_PATH": str(observed_path),
            },
            pi_child_wave_timeout_seconds=12.5,
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            pi_extension_entrypoints=managed_entrypoints,
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
    assert argv.count("--mode") == 1
    assert argv[argv.index("--mode") + 1] == "rpc"
    assert argv.count("--session-dir") == 1
    assert argv[argv.index("--session-dir") + 1] == str(scoped_session_dir)
    assert "--no-extensions" in argv
    assert "--no-skills" in argv
    assert "--no-context-files" in argv
    assert "--no-prompt-templates" in argv
    assert [argv[index + 1] for index, token in enumerate(argv) if token == "-e"] == list(
        managed_entrypoints
    )
    assert "node" not in argv
    assert "bun" not in argv
    assert "meridian-pi" not in argv

    observed_env = observed["env"]
    assert observed_env["MERIDIAN_PI_BINARY"] == str(fake_pi)
    assert observed_env["MERIDIAN_PI_SESSION_ROLE"] == "spawned"
    assert observed_env["MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS"] == "12500"
    assert observed_env["MERIDIAN_PI_LIFECYCLE_EVENT_FILE"] == str(
        _lifecycle_sidecar_path(tmp_path, SpawnId("p-pi-direct-runtime"))
    )
    assert observed_env["PI_CODING_AGENT_SESSION_DIR"] == str(scoped_session_dir)
    assert "PI_CODING_AGENT_DIR" not in observed_env

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
    await connection.start(
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
async def test_pi_rpc_connection_ingests_lifecycle_sidecar_events_and_tees_stderr_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-sidecar-lifecycle")
    lifecycle_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-sidecar-lifecycle","correlation_id":"j-1",'
        '"subspawn_id":"j-1","emitted_at_ms":1760000000000}'
    )
    plain_stderr = "warning from stderr"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-sidecar-events\"}'\n"
        f"printf '%s\\n' '{plain_stderr}' >&2\n"
        f"printf '%s\\n' '{lifecycle_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
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
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

    connection = PiRpcConnection()
    await connection.start(
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

    lifecycle_events = [
        event for event in non_phase_events if event.event_type == "meridian.subspawn.start"
    ]
    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].payload["subspawn_id"] == "j-1"
    assert lifecycle_events[0].payload["parent_spawn_id"] == str(spawn_id)
    assert not any(
        event.event_type == "meridian.lifecycle.parse_error" for event in non_phase_events
    )
    stderr_log = resolve_spawn_log_dir(tmp_path, spawn_id) / "stderr.log"
    stderr_text = stderr_log.read_text(encoding="utf-8")
    assert plain_stderr in stderr_text
    assert lifecycle_line not in stderr_text
    assert lifecycle_line in sidecar_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_flushes_sidecar_events_written_after_agent_end_before_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-sidecar-eof-catchup")
    trailing_lifecycle_line = (
        '{"type":"meridian.subspawn.end","schema_version":1,'
        '"parent_spawn_id":"p-pi-sidecar-eof-catchup","correlation_id":"j-eof-end",'
        '"subspawn_id":"j-eof","status":"succeeded","success":true,'
        '"emitted_at_ms":1760000000001}'
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-sidecar-eof-catchup\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"agent_start\"}'\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      sleep 0.05\n"
        f"      printf '%s\\n' '{trailing_lifecycle_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
        "      exit 0\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

    connection = PiRpcConnection()
    await connection.start(
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

    non_phase_events = [
        event async for event in connection.events() if not _is_pi_phase_event(event)
    ]
    lifecycle_events = [
        event
        for event in non_phase_events
        if event.event_type == "meridian.subspawn.end"
    ]
    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].payload["subspawn_id"] == "j-eof"
    assert non_phase_events.index(lifecycle_events[0]) > max(
        index
        for index, event in enumerate(non_phase_events)
        if event.event_type == "agent_end"
    )
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
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-stderr-ignore","correlation_id":"j-stderr",'
        '"subspawn_id":"j-stderr","emitted_at_ms":1760000000000}'
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
    await connection.start(
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
    assert not any(event.event_type == "meridian.subspawn.start" for event in non_phase_events)
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
async def test_pi_rpc_connection_lifecycle_sidecar_parent_mismatch_emits_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-parent-mismatch")
    mismatched_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-someone-else","correlation_id":"j-2",'
        '"subspawn_id":"j-2","emitted_at_ms":1760000000000}'
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-parent-mismatch\"}'\n"
        f"printf '%s\\n' '{mismatched_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
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
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

    connection = PiRpcConnection()
    await connection.start(
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

    assert not any(event.event_type == "meridian.subspawn.start" for event in non_phase_events)
    parse_errors = [
        event
        for event in non_phase_events
        if event.event_type == "meridian.lifecycle.parse_error"
    ]
    assert len(parse_errors) == 1
    assert parse_errors[0].payload["reason"] == "parent_spawn_id_mismatch"
    assert parse_errors[0].payload["raw_type"] == "meridian.subspawn.start"
    assert parse_errors[0].payload["raw_line"] == mismatched_line
    assert mismatched_line in sidecar_path.read_text(encoding="utf-8")
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_malformed_canonical_event_fails_closed_through_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    # Spawned Pi RPC sessions must stay alive long enough to receive Meridian's
    # initial prompt. Emitting the malformed sidecar event before reading stdin
    # races connection startup instead of exercising manager fail-closed logic.
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*)\n"
        "      printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-malformed\"}'\n"
        "      printf '%s\\n' '{\"type\":\"message_update\",\"delta\":\"before malformed\"}'\n"
        "      printf '%s\\n' "
        "'{\"type\":\"meridian.subspawn.start\",\"schema_version\":2}' >> "
        f"\"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
        "      printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\",\"stopReason\":\"stop\"}]}'\n"
        "      exit 0\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    sidecar_path = _lifecycle_sidecar_path(tmp_path, SpawnId("p-pi-live-malformed"))
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

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
async def test_pi_spawn_manager_ignores_stdout_lifecycle_and_uses_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-stdout-sidecar-dedup")
    start_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-stdout-sidecar-dedup","correlation_id":"corr-start",'
        '"subspawn_id":"j-dupe","emitted_at_ms":1760000000000}'
    )
    end_line = (
        '{"type":"meridian.subspawn.end","schema_version":1,'
        '"parent_spawn_id":"p-pi-stdout-sidecar-dedup","correlation_id":"corr-end",'
        '"subspawn_id":"j-dupe","emitted_at_ms":1760000000001}'
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-dual-surface\"}'\n"
        f"printf '%s\\n' '{start_line}'\n"
        f"printf '%s\\n' '{start_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
        f"printf '%s\\n' '{end_line}'\n"
        f"printf '%s\\n' '{end_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
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
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

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

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
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
        assert outcome.status == "succeeded"

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        starts = [
            event
            for event in history
            if event["event_type"] == "meridian.subspawn.start"
            and event["payload"]["subspawn_id"] == "j-dupe"
        ]
        ends = [
            event
            for event in history
            if event["event_type"] == "meridian.subspawn.end"
            and event["payload"]["subspawn_id"] == "j-dupe"
        ]
        assert len(starts) == 1
        assert len(ends) == 1

        assert start_line in sidecar_path.read_text(encoding="utf-8")
        assert end_line in sidecar_path.read_text(encoding="utf-8")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_lifecycle_sidecar_blocks_until_notification_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)

    spawn_id = SpawnId("p-pi-sidecar-only-manager")
    lifecycle_lines = {
        "start": (
            '{"type":"meridian.subspawn.start","schema_version":1,'
            '"parent_spawn_id":"p-pi-sidecar-only-manager","correlation_id":"j-1-start",'
            '"subspawn_id":"j-1","wait_policy":"tracked","emitted_at_ms":1760000000000}'
        ),
        "end": (
            '{"type":"meridian.subspawn.end","schema_version":1,'
            '"parent_spawn_id":"p-pi-sidecar-only-manager","correlation_id":"j-1-end",'
            '"subspawn_id":"j-1","wait_policy":"tracked","emitted_at_ms":1760000000001}'
        ),
        "queued": (
            '{"type":"meridian.notification.queued","schema_version":1,'
            '"parent_spawn_id":"p-pi-sidecar-only-manager","correlation_id":"n-1-queued",'
            '"notification_id":"n-1","emitted_at_ms":1760000000002}'
        ),
        "delivered": (
            '{"type":"meridian.notification.delivered","schema_version":1,'
            '"parent_spawn_id":"p-pi-sidecar-only-manager","correlation_id":"n-1-delivered",'
            '"notification_id":"n-1","emitted_at_ms":1760000000003}'
        ),
        "completed": (
            '{"type":"meridian.notification.completed","schema_version":1,'
            '"parent_spawn_id":"p-pi-sidecar-only-manager","correlation_id":"n-1-completed",'
            '"notification_id":"n-1","emitted_at_ms":1760000000004}'
        ),
    }

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
        "    print('pi 1.2.3')\n"
        "    raise SystemExit(0)\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--help':\n"
        f"    print({json.dumps(_PI_HELP_SURFACE)})\n"
        "    raise SystemExit(0)\n"
        "def emit_stdout(payload):\n"
        "    print(json.dumps(payload), flush=True)\n"
        "emit_stdout({'type': 'session', 'id': 'ses-sidecar-only-manager'})\n"
        "terminal_event = {\n"
        "    'type': 'agent_end',\n"
        "    'messages': [{'role': 'assistant', 'stopReason': 'stop'}],\n"
        "}\n"
        "sidecar_path = os.environ.get('MERIDIAN_PI_LIFECYCLE_EVENT_FILE', '').strip()\n"
        "if not sidecar_path:\n"
        "    raise RuntimeError('missing MERIDIAN_PI_LIFECYCLE_EVENT_FILE')\n"
        "def emit_sidecar(line):\n"
        "    with open(sidecar_path, 'a', encoding='utf-8') as handle:\n"
        "        handle.write(line + '\\n')\n"
        "for line in sys.stdin:\n"
        "    payload = json.loads(line)\n"
        "    payload_type = payload.get('type')\n"
        "    if payload_type == 'prompt':\n"
        f"        emit_sidecar({json.dumps(lifecycle_lines['start'])})\n"
        "        time.sleep(0.03)\n"
        "        emit_stdout({'type': 'agent_start'})\n"
        "        emit_stdout(terminal_event)\n"
        "        time.sleep(0.03)\n"
        f"        emit_sidecar({json.dumps(lifecycle_lines['end'])})\n"
        f"        emit_sidecar({json.dumps(lifecycle_lines['queued'])})\n"
        f"        emit_sidecar({json.dumps(lifecycle_lines['delivered'])})\n"
        "        time.sleep(0.03)\n"
        "        emit_stdout({'type': 'agent_start'})\n"
        "        emit_stdout(terminal_event)\n"
        f"        emit_sidecar({json.dumps(lifecycle_lines['completed'])})\n"
        "        continue\n"
        "    if payload_type == 'abort':\n"
        "        raise SystemExit(0)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

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

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
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
        assert outcome.status == "succeeded"

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phases = [
            event["payload"]["phase"]
            for event in history
            if event["event_type"] == "meridian.pi.lifecycle.phase"
        ]
        assert "waiting_for_tracked_children" in phases
        assert "waiting_for_notification_completion" in phases
        agent_end_indices = [
            index for index, event in enumerate(history) if event["event_type"] == "agent_end"
        ]
        finalized_index = next(
            index
            for index, event in enumerate(history)
            if event["event_type"] == "meridian.pi.lifecycle.phase"
            and event["payload"]["phase"] == "finalized"
        )
        waiting_child_index = next(
            index
            for index, event in enumerate(history)
            if event["event_type"] == "meridian.pi.lifecycle.phase"
            and event["payload"]["phase"] == "waiting_for_tracked_children"
        )
        completed_index = next(
            index
            for index, event in enumerate(history)
            if event["event_type"] == "meridian.notification.completed"
            and event["payload"]["notification_id"] == "n-1"
        )
        assert agent_end_indices
        assert waiting_child_index < finalized_index
        assert finalized_index > completed_index

        for event_type, event_id_field, event_id in [
            ("meridian.subspawn.start", "subspawn_id", "j-1"),
            ("meridian.subspawn.end", "subspawn_id", "j-1"),
            ("meridian.notification.queued", "notification_id", "n-1"),
            ("meridian.notification.delivered", "notification_id", "n-1"),
            ("meridian.notification.completed", "notification_id", "n-1"),
        ]:
            matches = [
                event
                for event in history
                if event["event_type"] == event_type
                and event["payload"][event_id_field] == event_id
            ]
            assert len(matches) == 1

        for line in lifecycle_lines.values():
            assert line in sidecar_path.read_text(encoding="utf-8")
    finally:
        await manager.shutdown()


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
        assert [message["type"] for message in inbound_messages] == ["prompt"]
        assert inbound_messages[0]["message"] == "hello auto prompt"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_without_session_event_still_quiesces_and_records_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS", 0.2)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    inbound_log = tmp_path / "pi-inbound.jsonl"
    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
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

    spawn_id = SpawnId("p-pi-no-session-phase")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello no session",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello no session",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        assert outcome.status == "succeeded"
        assert outcome.error is None

        connection = manager.get_connection(spawn_id)
        assert connection is None or connection.session_id is None

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phases = [
            event["payload"]["phase"]
            for event in history
            if event["event_type"] == "meridian.pi.lifecycle.phase"
        ]
        assert "initial_prompt_sent" in phases
        assert "waiting_for_first_pi_event_after_prompt" in phases
        assert "first_pi_event_received" in phases
        assert "session_event_absent" in phases
        assert "quiescence_micro_drain_started" in phases
        assert "finalized" in phases

        inbound_messages = [
            json.loads(line)
            for line in inbound_log.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [message["type"] for message in inbound_messages] == ["prompt"]
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

    spawn_id = SpawnId("p-pi-prompt-failed-response")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
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
        assert outcome.error == "No API key configured"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_spawn_manager_first_event_timeout_fails_and_records_timeout_phase(
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
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"type\":\"prompt\"'*) sleep 30 ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
        "  esac\n"
        "done\n",
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

    spawn_id = SpawnId("p-pi-first-event-timeout-manager")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )

    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello timeout",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello timeout",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_rpc_no_response_after_initial_prompt"

        history_path = tmp_path / "spawns" / str(spawn_id) / "history.jsonl"
        history = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phases = [
            event["payload"]["phase"]
            for event in history
            if event["event_type"] == "meridian.pi.lifecycle.phase"
        ]
        assert "waiting_for_first_pi_event_after_prompt" in phases
        assert "first_pi_event_timeout" in phases
        assert "finalized" in phases
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
    assert (await _next_non_phase_event(event_iter)).event_type == "session"
    await connection.stop(reason="test")
    _ = [event async for event in event_iter]

    assert observed_cwd.read_text(encoding="utf-8").strip() == str(task_cwd)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_lifecycle_sidecar_does_not_satisfy_first_stdout_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_KILL_GRACE_SECONDS", 0.01)

    spawn_id = SpawnId("p-pi-sidecar-no-stdout-timeout")
    lifecycle_line = (
        '{"type":"meridian.subspawn.start","schema_version":1,'
        '"parent_spawn_id":"p-pi-sidecar-no-stdout-timeout","correlation_id":"j-timeout",'
        '"subspawn_id":"j-timeout","emitted_at_ms":1760000000000}'
    )

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
        f"      printf '%s\\n' '{lifecycle_line}' >> \"${_PI_LIFECYCLE_EVENT_FILE_ENV}\"\n"
        "      sleep 30\n"
        "      ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    sidecar_path = _lifecycle_sidecar_path(tmp_path, spawn_id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.touch()
    monkeypatch.setenv(_PI_LIFECYCLE_EVENT_FILE_ENV, str(sidecar_path))

    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello timeout",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello timeout",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    events = [event async for event in connection.events()]
    lifecycle_events = [event for event in events if event.event_type == "meridian.subspawn.start"]
    assert len(lifecycle_events) == 1
    assert any(
        event.event_type == "error/connectionClosed"
        and event.payload.get("message") == "pi_rpc_no_response_after_initial_prompt"
        for event in events
    )
    assert connection.state == "failed"
    assert connection.subprocess_pid is None
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
        await connection.start(
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
