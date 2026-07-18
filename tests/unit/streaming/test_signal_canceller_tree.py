# qa-validated: reaper-escape-fix-test-cleanup
"""Behavior tests for SignalCanceller CLI cancellation dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.platform.process_scope import CleanupResult, ProcessScopeSnapshot
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts
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
    terminal = (
        TerminalFacts(
            exit_code=exit_code if exit_code is not None else 1,
            finished_at=started_at or "2024-01-01T00:00:00Z",
            published_at=started_at or "2024-01-01T00:00:00Z",
            origin=terminal_origin or "runner",  # type: ignore[arg-type]
        )
        if status in {"succeeded", "failed", "cancelled", "timed_out"}
        else None
    )
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
        kind="child",
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
        runner_exit=None,
        terminal=terminal,
    )


def _make_scope(
    scope_id: str,
    *,
    root_pid: int,
    owner_policy: str = "spawn_owned",
) -> ProcessScopeSnapshot:
    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
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
        patch("meridian.lib.streaming.signal_canceller.terminate_tree_sync"),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cancel_cli_spawn_does_not_run_legacy_worker_fallback_after_runner_signal(
    tmp_path: Path,
) -> None:
    """Post-runner containment cleanup should not unguardedly re-signal legacy worker_pid."""
    spawn_id = SpawnId("s-test")
    running_record = _make_record(runner_pid=777, worker_pid=888)

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
        patch("meridian.lib.streaming.signal_canceller.terminate_tree_sync"),
        patch(
            "meridian.lib.core.process_cleanup.read_scopes_from_disk",
            return_value=[],
        ),
        patch(
            "meridian.lib.core.process_cleanup.terminate_tree_sync",
            side_effect=AssertionError("legacy worker fallback should not run"),
        ),
    ):
        outcome = await canceller._cancel_cli_spawn(spawn_id, running_record)

    assert outcome.finalizing is True
    assert outcome.status == "finalizing"


@pytest.mark.asyncio
async def test_cleanup_spawn_scopes_uses_release_id_for_duplicate_labels(
    tmp_path: Path,
) -> None:
    first_backend = _make_scope("backend", root_pid=101)
    second_backend = _make_scope("backend", root_pid=202)
    session_backend = _make_scope(
        "backend",
        root_pid=303,
        owner_policy="session_owned",
    )
    terminated_release_ids: list[str] = []
    marked_release_ids: list[str] = []

    def _terminate_scope(scope: ProcessScopeSnapshot, **kwargs: object) -> CleanupResult:
        terminated_release_ids.append(scope.release_id)
        return CleanupResult(
            scope_id=scope.scope_id,
            root_pid=scope.root_pid,
            descendant_count=0,
            reason=str(kwargs["reason"]),
            grace_seconds=0.0,
            kill_escalated=False,
            degraded_fallback=False,
            skip_reason=None,
        )

    canceller = SignalCanceller(runtime_root=tmp_path, grace_seconds=0.0)

    with (
        patch(
            "meridian.lib.core.process_cleanup.read_scopes_from_disk",
            return_value=[first_backend, second_backend, session_backend],
        ),
        patch(
            "meridian.lib.core.process_cleanup.is_scope_released",
            side_effect=lambda _root, _sid, release_id: release_id
            == first_backend.release_id,
        ),
        patch(
            "meridian.lib.core.process_cleanup.psutil.Process",
            return_value=type("LiveProc", (), {"create_time": lambda self: 1_700_000_000.0})(),
        ),
        patch(
            "meridian.lib.core.process_cleanup.terminate_scope_sync",
            side_effect=_terminate_scope,
        ),
        patch(
            "meridian.lib.core.process_cleanup.mark_scope_released",
            side_effect=lambda _root, _sid, release_id: marked_release_ids.append(
                release_id
            ),
        ),
    ):
        await canceller._cleanup_spawn_scopes(_make_record())

    assert first_backend.scope_id == second_backend.scope_id
    assert terminated_release_ids == [second_backend.release_id]
    assert marked_release_ids == [second_backend.release_id]
