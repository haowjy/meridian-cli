# qa-validated: pi-rpc-quiescence
"""Pi harness wiring tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeIdentity
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    RawHarnessEvent,
)
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import (
    LaunchPaths,
    resolve_project_runtime_root_for_write,
)
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


















