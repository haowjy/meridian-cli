"""Tests that SignalCanceller._cancel_cli_spawn uses terminate_tree_sync.

# qa-validated: test-suite-redesign
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.streaming.signal_canceller import SignalCanceller


def _make_record(
    *,
    status: str = "running",
    launch_mode: str = "background",
    runner_pid: int | None = 12345,
    worker_pid: int | None = None,
    started_at: str | None = "2024-01-01T00:00:00Z",
    exit_code: int | None = None,
    terminal_origin: str | None = None,
) -> SpawnRecord:
    return SpawnRecord(
        id="s-test",
        chat_id=None,
        parent_id=None,
        model=None,
        agent=None,
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness=None,
        kind="spawn",
        desc=None,
        work_id=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode=launch_mode,  # type: ignore[arg-type]
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        status=status,  # type: ignore[arg-type]
        prompt=None,
        started_at=started_at,
        exited_at=None,
        process_exit_code=None,
        finished_at=None,
        exit_code=exit_code,
        duration_secs=None,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        reasoning_tokens=None,
        cost_is_estimate=False,
        error=None,
        terminal_origin=terminal_origin,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_cancel_cli_spawn_uses_terminate_tree_sync(tmp_path: Path) -> None:
    """Cancellation via terminate_tree_sync produces a cancelled outcome."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=12345)
    cancelled_record = _make_record(
        status="cancelled",
        runner_pid=12345,
        exit_code=130,
        terminal_origin="cancel",
    )

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=1.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            side_effect=[running_record, cancelled_record],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.status == "cancelled"
    assert outcome.exit_code == 130


@pytest.mark.asyncio
async def test_cancel_cli_spawn_passes_started_epoch(tmp_path: Path) -> None:
    """Epoch derived from record.started_at is forwarded to the termination boundary.

    A state-capture function records what the boundary receives so the derivation
    contract can be verified without inspecting mock internals.
    """
    spawn_id = SpawnId("s-test")
    running_record = _make_record(
        runner_pid=99,
        started_at="2024-06-15T12:00:00Z",
    )
    cancelled_record = _make_record(
        status="cancelled",
        runner_pid=99,
        exit_code=130,
        terminal_origin="cancel",
    )

    captured_epochs: list[float] = []

    def _capture_epoch(pid: int, *, created_at_epoch: float, **_: object) -> None:
        captured_epochs.append(created_at_epoch)

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=2.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            side_effect=[running_record, cancelled_record],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
            side_effect=_capture_epoch,
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    # created_at_epoch must be a non-zero float derived from "2024-06-15T12:00:00Z"
    assert captured_epochs, "terminate_tree_sync must receive a created_at_epoch"
    epoch = captured_epochs[0]
    assert isinstance(epoch, float)
    assert epoch > 0.0
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_cli_spawn_returns_finalizing_when_no_terminal(tmp_path: Path) -> None:
    """Returns finalizing outcome when process exits but spawn record stays non-terminal."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=777)

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            return_value=running_record,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cancel_cli_spawn_no_os_kill(tmp_path: Path) -> None:
    """os.kill is not invoked during CLI spawn cancellation."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=12345)
    cancelled_record = _make_record(
        status="cancelled",
        runner_pid=12345,
        exit_code=130,
        terminal_origin="cancel",
    )

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=1.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.spawn_store.get_spawn",
            side_effect=[running_record, cancelled_record],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_process_alive",
            return_value=True,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
        ),
        patch("os.kill") as mock_os_kill,
    ):
        await canceller._cancel_cli_spawn(spawn_id, running_record)

    mock_os_kill.assert_not_called()
