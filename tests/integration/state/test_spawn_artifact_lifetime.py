"""Uncertain delivery must not be silently retried."""

from pathlib import Path

import pytest

from meridian.lib.core.delivery import DeliveryError
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.control_action import ControlActionCoordinator, ControlActionType
from meridian.lib.state.spawn_store import start_spawn
from tests.support.launch_paths import launch_paths


def _start_test_spawn(
    runtime_root: Path,
    spawn_id: str,
    *,
    status: SpawnStatus,
) -> None:
    start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.6",
        agent="coder",
        harness="codex",
        prompt="test",
        spawn_id=spawn_id,
        status=status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("request timed out"),
        ConnectionResetError("reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        RuntimeError("Codex websocket closed"),
        RuntimeError("pi_rpc_prompt_ack_timeout"),
    ],
)
async def test_regression_failure_after_send_is_not_resent_and_warns_sender(
    tmp_path: Path, failure: Exception
) -> None:
    """A failure that may follow the write is sent once, and the sender is told."""
    runtime_root = tmp_path / "runtime"
    _start_test_spawn(runtime_root, "p1", status="running")
    coordinator = ControlActionCoordinator(launch=launch_paths(runtime_root, "p1"))
    sends = 0

    async def _send() -> None:
        nonlocal sends
        sends += 1
        raise failure

    with pytest.raises(DeliveryError, match=r"may have arrived and was not resent"):
        await coordinator.run_action(
            action=ControlActionType.INJECT, payload={"text": "x"}, source="test", send=_send
        )

    assert sends == 1
