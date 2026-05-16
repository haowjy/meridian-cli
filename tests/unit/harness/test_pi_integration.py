"""Pi harness wiring tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import BootstrapMode, ForkMaterializationMode
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessEvent
from meridian.lib.harness.connections.pi_rpc import PiConnection
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.harness.semantics import activity_transition, clears_signal, terminal_outcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec, TerminalSurfaceMode
from meridian.lib.safety.permissions import PermissionConfig, UnsafeNoOpPermissionResolver


def test_pi_adapter_registered_with_expected_phase12_contract() -> None:
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
    assert Path(overrides["PI_CODING_AGENT_DIR"]).parts[-2:] == ("pi", "agent")
    assert overrides["MERIDIAN_PI_LAUNCH_CONFIG"] == "/tmp/pi-launch-config.json"


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

    assert terminal_outcome(success_event) is not None
    assert terminal_outcome(success_event).status == "succeeded"
    assert terminal_outcome(error_event) is not None
    assert terminal_outcome(error_event).status == "failed"
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
async def test_pi_connection_starts_print_mode_subprocess_and_drains_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "meridian-pi"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"session\",\"id\":\"ses-pi\"}'\n"
        "printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\","
        "\"stopReason\":\"stop\"}]}'\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    connection = PiConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-pi-connection"),
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

    events = [event async for event in connection.events()]

    assert [event.event_type for event in events] == ["session", "agent_end"]
    assert connection.session_id == "ses-pi"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_pi_connection_launches_in_task_cwd_when_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "printf '%s\\n' "
        "'{\"type\":\"agent_end\",\"messages\":[{\"role\":\"assistant\","
        "\"stopReason\":\"stop\"}]}'\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PI_TEST_CWD_FILE", str(observed_cwd))

    connection = PiConnection()
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
    _ = [event async for event in connection.events()]

    assert observed_cwd.read_text(encoding="utf-8").strip() == str(task_cwd)
