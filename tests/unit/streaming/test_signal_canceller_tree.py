# qa-validated: reaper-escape-fix-test-cleanup
"""Behavior tests for SignalCanceller CLI cancellation dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
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


def _make_scope(scope_id: str, *, root_pid: int) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy="spawn_owned",
        owner_id="s-test",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=root_pid,
        root_created_at_epoch=1_700_000_000.0,
        pgid=None,
        job_name=None,
        degraded_reason=None,
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


@pytest.mark.asyncio
async def test_cleanup_spawn_scopes_uses_release_id_for_duplicate_labels(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("s-test")
    first_backend = _make_scope("backend", root_pid=101)
    second_backend = _make_scope("backend", root_pid=202)
    terminated_release_ids: list[str] = []
    marked_release_ids: list[str] = []

    def _terminate_scope(scope: ProcessScopeSnapshot, **_: object) -> None:
        terminated_release_ids.append(scope.release_id)

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.streaming.signal_canceller.read_scopes_from_disk",
            return_value=[first_backend, second_backend],
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.is_scope_released",
            side_effect=lambda _root, _sid, release_id: release_id
            == first_backend.release_id,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.terminate_scope_sync",
            side_effect=_terminate_scope,
        ),
        patch(
            "meridian.lib.streaming.signal_canceller.mark_scope_released",
            side_effect=lambda _root, _sid, release_id: marked_release_ids.append(
                release_id
            ),
        ),
    ):
        await canceller._cleanup_spawn_scopes(spawn_id, _make_record())

    assert first_backend.scope_id == second_backend.scope_id
    assert terminated_release_ids == [second_backend.release_id]
    assert marked_release_ids == [second_backend.release_id]
