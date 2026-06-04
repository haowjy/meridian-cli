# qa-validated: reaper-escape-fix-test-cleanup
"""Behavior tests for SignalCanceller CLI cancellation dispatch."""

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
        runner_created_at_epoch=None,
        status=status,  # type: ignore[arg-type]
        prompt=None,
        started_at=started_at,
        last_attempt_exited_at=None,
        last_attempt_exit_code=None,
        runner_exit_code=None,
        runner_exit_status=None,
        runner_exit_error=None,
        runner_exit_at=None,
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
async def test_cancel_cli_spawn_legacy_path_uses_tree_termination_with_started_epoch(
    tmp_path: Path,
) -> None:
    """Without scope records, cancel falls back to runner tree termination."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=99, started_at="2024-06-15T12:00:00Z")
    cancelled_record = _make_record(
        status="cancelled",
        runner_pid=99,
        exit_code=130,
        terminal_origin="cancel",
    )

    captured: list[tuple[int, float, str]] = []

    def _capture_tree(pid: int, *, created_at_epoch: float, scope_id: str, **_: object) -> None:
        captured.append((pid, created_at_epoch, scope_id))

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
            "meridian.lib.streaming.signal_canceller.read_scopes_from_disk",
            return_value=[],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_tree_sync",
            side_effect=_capture_tree,
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert captured and captured[0][0] == 99
    assert captured[0][1] > 0.0
    assert captured[0][2] == "s-test:runner"
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 130


@pytest.mark.asyncio
async def test_cancel_cli_spawn_returns_finalizing_when_terminal_state_never_arrives(
    tmp_path: Path,
) -> None:
    """Cancellation should surface finalizing when durable terminal state does not arrive."""
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
            "meridian.lib.streaming.signal_canceller.read_scopes_from_disk",
            return_value=[],
        ),
        patch("meridian.lib.streaming.signal_canceller.terminate_tree_sync"),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"
