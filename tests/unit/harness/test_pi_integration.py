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
from meridian.lib.harness.connections.base import ConnectionConfig, ConnectionNotReady, HarnessEvent
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.pi_runtime_resolver import PiRuntimeResolution
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.harness.semantics import activity_transition, clears_signal, terminal_outcome
from meridian.lib.launch.env import build_harness_env_overrides
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.launch.request import SessionRequest
from meridian.lib.safety.permissions import PermissionConfig, UnsafeNoOpPermissionResolver
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
    assert contract.extraction.session_observation_order == (
        "artifacts",
        "primary_detection",
        "current_session",
    )
    assert contract.capabilities.supports_session_fork is True
    assert contract.capabilities.supports_primary_launch is True
    assert contract.capabilities.terminal_surface_modes == (
        TerminalSurfaceMode.PTY_MEDIATED,
        TerminalSurfaceMode.NATIVE_INHERIT,
    )
    assert contract.capabilities.default_terminal_surface_mode is TerminalSurfaceMode.PTY_MEDIATED

    adapter = registry.get_subprocess_harness(HarnessId.PI)
    overrides = adapter.env_overrides(PermissionConfig())
    assert "PI_CODING_AGENT_DIR" not in overrides
    assert Path(overrides["PI_CODING_AGENT_SESSION_DIR"]).parts[-2:] == (
        "meridian-pi",
        "sessions",
    )


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


def test_pi_prepare_prelaunch_scopes_spawned_session_dir_for_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HarnessRegistry.with_defaults()
    adapter = registry.get_subprocess_harness(HarnessId.PI)

    def _resolve_runtime(**_kwargs: object) -> PiRuntimeResolution:
        return PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 1.2.3",
        )

    monkeypatch.setattr("meridian.lib.harness.pi.resolve_pi_runtime", _resolve_runtime)
    child_env = {
        "MERIDIAN_PI_SESSION_ROLE": "spawned",
        "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "pi-sessions"),
    }

    prelaunch = adapter.prepare_prelaunch(
        runtime_root=tmp_path / ".runtime",
        spawn_id=SpawnId("p-pi-prelaunch-scope"),
        session=SessionRequest(),
        child_cwd=tmp_path,
        child_env=child_env,
        resolved_harness_session_id="",
    )

    expected_scoped = tmp_path / "pi-sessions" / "p-pi-prelaunch-scope"
    assert child_env["PI_CODING_AGENT_SESSION_DIR"] == str(expected_scoped)
    assert prelaunch.env_overrides["PI_CODING_AGENT_SESSION_DIR"] == str(expected_scoped)
    assert prelaunch.metadata["pi_runtime_session_dir"] == str(expected_scoped)


def test_pi_prepare_prelaunch_does_not_double_append_spawn_id_in_session_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HarnessRegistry.with_defaults()
    adapter = registry.get_subprocess_harness(HarnessId.PI)

    def _resolve_runtime(**_kwargs: object) -> PiRuntimeResolution:
        return PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 1.2.3",
        )

    monkeypatch.setattr("meridian.lib.harness.pi.resolve_pi_runtime", _resolve_runtime)
    already_scoped = tmp_path / "pi-sessions" / "p-pi-prelaunch-idempotent"
    child_env = {
        "MERIDIAN_PI_SESSION_ROLE": "spawned",
        "PI_CODING_AGENT_SESSION_DIR": str(already_scoped),
    }

    prelaunch = adapter.prepare_prelaunch(
        runtime_root=tmp_path / ".runtime",
        spawn_id=SpawnId("p-pi-prelaunch-idempotent"),
        session=SessionRequest(),
        child_cwd=tmp_path,
        child_env=child_env,
        resolved_harness_session_id="",
    )

    assert child_env["PI_CODING_AGENT_SESSION_DIR"] == str(already_scoped)
    assert prelaunch.env_overrides["PI_CODING_AGENT_SESSION_DIR"] == str(already_scoped)
    assert prelaunch.metadata["pi_runtime_session_dir"] == str(already_scoped)


def test_pi_adapter_build_command_uses_rpc_mode_for_spawned_and_native_primary_runs(
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
    assert "--no-extensions" in spawned_command
    assert "--no-skills" in spawned_command
    assert "--no-context-files" in spawned_command
    assert "--no-prompt-templates" in spawned_command
    assert Path(spawned_command[spawned_command.index("--session-dir") + 1]).parts[-2:] == (
        "meridian-pi",
        "sessions",
    )
    spawned_extensions = [
        spawned_command[index + 1]
        for index, token in enumerate(spawned_command)
        if token == "-e"
    ]
    assert spawned_extensions == [
        str(tmp_path / "agent" / "extensions" / "managed-bash" / "index.js"),
        str(tmp_path / "agent" / "extensions" / "meridian-lifecycle" / "index.js"),
    ]

    primary_command = adapter.build_command(
        SpawnParams(prompt="hello from primary", interactive=True),
        perms,
    )
    assert "--mode" not in primary_command
    assert "--no-extensions" not in primary_command
    assert "--no-skills" not in primary_command
    assert "--no-context-files" not in primary_command
    assert "--no-prompt-templates" not in primary_command
    assert "-e" not in primary_command
    assert "--extension" not in primary_command

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


@pytest.mark.asyncio
async def test_pi_rpc_connection_requires_non_empty_prompt_for_spawned_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = PiRpcConnection()
    subprocess_started = False

    async def _unexpected_start_subprocess(*args: object, **kwargs: object) -> None:
        nonlocal subprocess_started
        _ = args, kwargs
        subprocess_started = True
        raise AssertionError("subprocess should not start for empty spawned prompt")

    monkeypatch.setattr(connection, "_start_subprocess", _unexpected_start_subprocess)

    with pytest.raises(ValueError, match=r"^pi_rpc_spawned_prompt_required$"):
        await connection.start(
            ConnectionConfig(
                spawn_id=SpawnId("p-pi-spawned-empty-prompt"),
                harness_id=HarnessId.PI,
                prompt=" \n\t ",
                control_root=tmp_path,
                env_overrides={},
                pi_session_role="spawned",
            ),
            ResolvedLaunchSpec(
                harness=HarnessId.PI,
                prompt=" \n\t ",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
        )

    assert subprocess_started is False
    assert connection.state == "created"
    assert connection.subprocess_pid is None


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
    assert observed_env["PI_CODING_AGENT_SESSION_DIR"] == str(scoped_session_dir)
    assert "PI_CODING_AGENT_DIR" not in observed_env


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

    shim = bin_dir / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'pi 1.2.3'; exit 0; fi\n"
        f"if [ \"$1\" = \"--help\" ]; then echo '{_PI_HELP_SURFACE}'; exit 0; fi\n"
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
    assert (await _next_non_phase_event(event_iter)).event_type == "session"

    progress_updates: list[tuple[str, dict[str, object]]] = []

    async def _capture_progress(stage: str, payload: dict[str, object]) -> None:
        progress_updates.append((stage, payload))

    stop_result = await connection.stop(reason="quiescent", progress=_capture_progress)

    inbound_messages = [
        json.loads(line) for line in inbound_log.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [message["type"] for message in inbound_messages] == ["prompt", "abort"]
    assert inbound_messages[0]["message"] == "hello"
    assert stop_result.escalated is True
    assert progress_updates == [
        ("quiescent_stop_escalating", {"reason": "abort_grace_expired"}),
    ]
    assert connection.state == "stopped"
    assert connection.subprocess_pid is None
    with pytest.raises(StopAsyncIteration):
        await anext(event_iter)


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
async def test_pi_rpc_connection_malformed_canonical_event_fails_closed_through_manager(
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
async def test_pi_spawn_manager_first_event_eof_after_initial_prompt_fails_with_timeout_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
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
        "    *'\"type\":\"prompt\"'*) exit 0 ;;\n"
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

    spawn_id = SpawnId("p-pi-first-event-eof-manager")
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
            prompt="hello eof",
            control_root=tmp_path,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello eof",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=1.0)
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
        assert "first_pi_event_eof_before_response" in phases
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
    assert (await _next_non_phase_event(event_iter)).event_type == "session"
    await connection.stop(reason="test")
    _ = [event async for event in event_iter]

    assert observed_cwd.read_text(encoding="utf-8").strip() == str(task_cwd)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_rpc_connection_times_out_waiting_for_first_event_after_initial_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    monkeypatch.setattr(pi_rpc_module, "_FIRST_STDOUT_AFTER_INITIAL_PROMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(pi_rpc_module, "_PROCESS_KILL_GRACE_SECONDS", 0.01)

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
        "    *'\"type\":\"prompt\"'*) sleep 30 ;;\n"
        "    *'\"type\":\"abort\"'*) exit 0 ;;\n"
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
            spawn_id=SpawnId("p-pi-first-event-timeout"),
            harness_id=HarnessId.PI,
            prompt="hello timeout",
            control_root=tmp_path,
            env_overrides={},
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello timeout",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    events = [event async for event in connection.events()]
    assert any(
        event.event_type == "error/connectionClosed"
        and event.payload.get("message") == "pi_rpc_no_response_after_initial_prompt"
        for event in events
    )
    assert connection.state == "failed"
    assert connection.subprocess_pid is None
    assert connection.session_id is None


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


@pytest.mark.asyncio
async def test_pi_spawn_manager_runtime_resolution_failure_propagates_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_extension_projection(monkeypatch, tmp_path)
    expected_error = "runtime probe failed before launch"

    def _fail_resolve_runtime(*, env: dict[str, str], role: str) -> object:
        _ = env, role
        raise pi_rpc_module.PiRuntimeResolutionError(expected_error)

    monkeypatch.setattr(pi_rpc_module, "resolve_pi_runtime", _fail_resolve_runtime)

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

    spawn_id = SpawnId("p-pi-runtime-error-manager")
    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=_start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )

    with pytest.raises(ConnectionNotReady, match=rf"^{expected_error}$"):
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

    await manager.shutdown()
