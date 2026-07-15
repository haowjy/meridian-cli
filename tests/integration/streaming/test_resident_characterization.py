"""Fake-clock characterization of resident completion precedence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming import resident_drain as resident_drain_module
from meridian.lib.streaming.drain_policy import DrainAction
from meridian.lib.streaming.resident_drain import ResidentDrainCoordinator
from tests.support.async_determinism import FakeClock, TaskGate
from tests.support.resident_drain import FakeResidentConnection, resident_event, start_row

_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)
_EVENT = resident_event(HarnessId.CODEX, "turn/completed", {})


def _coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deadline_seconds: float = 10.0,
) -> tuple[ResidentDrainCoordinator, FakeResidentConnection, FakeClock]:
    clock = FakeClock(start=100.0)
    monkeypatch.setattr(resident_drain_module.time, "monotonic", clock.monotonic)
    start_row(tmp_path, "p1", HarnessId.CODEX, None)
    connection = FakeResidentConnection(HarnessId.CODEX)
    coordinator = ResidentDrainCoordinator.for_connection(
        project_root=tmp_path,
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        receiver=connection,
        resident_backend=connection.resident_backend,
        deadline_seconds=deadline_seconds,
        poll_seconds=1.0,
    )
    return coordinator, connection, clock


def _install_cleanup_recorder(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[SpawnId],
) -> None:
    from meridian.lib.bootstrap import services as bootstrap_services

    class _CleanupService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            calls.append(root_id)
            return set()

    def _build_cleanup_service(
        _project_root: Path,
        _runtime_root: Path,
    ) -> _CleanupService:
        return _CleanupService()

    monkeypatch.setattr(
        bootstrap_services,
        "build_spawn_application_service_from_roots",
        _build_cleanup_service,
    )


@pytest.mark.asyncio
async def test_terminal_nonterminating_action_wins_and_clears_resident_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    waiting = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert waiting.emit_turn_boundary is True

    clock.advance(1.0)
    cancelled = TerminalEventOutcome(
        status="cancelled",
        exit_code=130,
        error="cancelled",
    )
    decision = await coordinator.handle_terminal_event(
        _EVENT,
        cancelled,
        DrainAction(terminate=False, emit_turn_boundary=True),
    )

    assert decision.recorded_outcome is None
    assert decision.emit_turn_boundary is True
    assert coordinator.handle_close(intentional_stop=False) is None
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_terminal_cancel_wins_over_done_and_outstanding_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    initial = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert initial.emit_turn_boundary is True

    clock.advance(1.0)
    write_spawn_signal(tmp_path, "p1", "done")
    cancelled = TerminalEventOutcome(
        status="cancelled",
        exit_code=130,
        error="explicit_cancel",
    )
    decision = await coordinator.handle_terminal_event(_EVENT, cancelled, _TERMINATE)

    assert decision.recorded_outcome == cancelled
    assert decision.emit_turn_boundary is False
    assert coordinator.handle_close(intentional_stop=False) is None
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_terminal_done_overrides_rearm_and_persisted_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current done override accepts success despite an active persisted child."""
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    clock.advance(1.0)
    write_spawn_signal(tmp_path, "p1", "done")
    write_spawn_signal(tmp_path, "p1", "rearm")

    decision = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)

    assert decision.recorded_outcome == _SUCCESS
    assert decision.emit_turn_boundary is False
    assert connection.fake_resident_backend.awaiting_done_values == [False]
    assert connection.fake_resident_backend.injected_messages == []


@pytest.mark.asyncio
async def test_terminal_rearm_resets_deadline_and_holds_ready_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current rearm behavior replaces, rather than preserves, the old deadline."""
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    initial = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert initial.emit_turn_boundary is True

    clock.advance(5.0)
    coordinator.observe_activity_transition("turn_active")
    spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p2"),
        "succeeded",
        0,
        origin="runner",
    )
    write_spawn_signal(tmp_path, "p1", "rearm")
    rearmed = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert rearmed.recorded_outcome is None
    assert rearmed.emit_turn_boundary is True

    clock.advance(5.0)
    at_old_deadline = await coordinator.handle_timeout()
    assert at_old_deadline.recorded_outcome is None

    clock.advance(5.0)
    cleanup_calls: list[SpawnId] = []
    _install_cleanup_recorder(monkeypatch, cleanup_calls)
    at_reset_deadline = await coordinator.handle_timeout()
    assert at_reset_deadline.recorded_outcome is not None
    assert at_reset_deadline.recorded_outcome.status == "timed_out"
    assert at_reset_deadline.recorded_outcome.exit_code == 1
    assert at_reset_deadline.recorded_outcome.error == "resident_deadline_expired"
    assert cleanup_calls == [SpawnId("p1")]
    assert connection.fake_resident_backend.awaiting_done_values == [True, False, True, False]


@pytest.mark.asyncio
async def test_terminal_wait_and_poll_see_late_grandchild_through_terminal_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resident assessment is transitive through a terminal direct parent."""
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p2"),
        "succeeded",
        0,
        origin="runner",
    )
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")

    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.recorded_outcome is None
    assert terminal.emit_turn_boundary is True
    assert connection.fake_resident_backend.awaiting_done_values == [True]

    clock.advance(1.0)
    still_waiting = await coordinator.handle_timeout()
    assert still_waiting.recorded_outcome is None
    assert connection.fake_resident_backend.injected_messages == []

    spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p3"),
        "succeeded",
        0,
        origin="runner",
    )
    clock.advance(1.0)
    ready = await coordinator.handle_timeout()
    assert ready.recorded_outcome == _SUCCESS
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_terminal_ready_tree_finalizes_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    clock.advance(1.0)

    decision = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)

    assert decision.recorded_outcome == _SUCCESS
    assert decision.emit_turn_boundary is False
    assert connection.fake_resident_backend.awaiting_done_values == [False]


@pytest.mark.asyncio
async def test_expired_deadline_beats_freshly_ready_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p2"),
        "succeeded",
        0,
        origin="runner",
    )
    cleanup_calls: list[SpawnId] = []
    _install_cleanup_recorder(monkeypatch, cleanup_calls)
    clock.advance(10.0)
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.exit_code == 1
    assert decision.recorded_outcome.error == "resident_deadline_expired"
    assert cleanup_calls == [SpawnId("p1")]
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_wait_done_signal_beats_already_expired_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    cleanup_calls: list[SpawnId] = []
    _install_cleanup_recorder(monkeypatch, cleanup_calls)
    clock.advance(10.0)
    write_spawn_signal(tmp_path, "p1", "done")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome == _SUCCESS
    assert cleanup_calls == []
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_followup_turn_active_defers_ready_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    spawn_store.finalize_spawn(
        tmp_path,
        SpawnId("p2"),
        "succeeded",
        0,
        origin="runner",
    )
    clock.advance(1.0)
    coordinator.observe_activity_transition("turn_active")
    decision = await coordinator.handle_timeout()

    assert decision.recorded_outcome is None
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]
    assert connection.fake_resident_backend.injected_messages == []


@pytest.mark.asyncio
async def test_plain_terminal_deadline_is_not_reset_by_later_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current plain-terminal behavior sets one deadline; polls do not extend it."""
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    clock.advance(4.0)
    first_poll = await coordinator.handle_timeout()
    assert first_poll.recorded_outcome is None
    clock.advance(5.0)
    second_poll = await coordinator.handle_timeout()
    assert second_poll.recorded_outcome is None

    cleanup_calls: list[SpawnId] = []
    _install_cleanup_recorder(monkeypatch, cleanup_calls)
    clock.advance(1.0)
    deadline = await coordinator.handle_timeout()
    assert deadline.recorded_outcome is not None
    assert deadline.recorded_outcome.status == "timed_out"
    assert deadline.recorded_outcome.error == "resident_deadline_expired"
    assert cleanup_calls == [SpawnId("p1")]
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]

    later_poll = await coordinator.handle_timeout()
    assert later_poll.recorded_outcome is None
    assert cleanup_calls == [SpawnId("p1")]


@pytest.mark.asyncio
async def test_deadline_outcome_wins_when_explicit_descendant_cancel_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    from meridian.lib.bootstrap import services as bootstrap_services

    cancel_calls: list[SpawnId] = []
    first_cancel_entered = asyncio.Event()
    release_cancels = TaskGate()

    class _CanonicalCancelService:
        async def cancel_descendants(self, root_id: SpawnId) -> set[str]:
            cancel_calls.append(root_id)
            if len(cancel_calls) == 1:
                first_cancel_entered.set()
                await release_cancels.wait_open()
            else:
                release_cancels.open()
            child = spawn_store.get_spawn(tmp_path, SpawnId("p2"))
            assert child is not None
            if child.status == "running":
                spawn_store.finalize_spawn(
                    tmp_path,
                    SpawnId("p2"),
                    "cancelled",
                    130,
                    origin="cancel",
                    error="cancelled",
                )
                return {"p2"}
            return set()

    service = _CanonicalCancelService()

    def _build_cancel_service(
        _project_root: Path,
        _runtime_root: Path,
    ) -> _CanonicalCancelService:
        return service

    monkeypatch.setattr(
        bootstrap_services,
        "build_spawn_application_service_from_roots",
        _build_cancel_service,
    )
    clock.advance(10.0)
    explicit_cancel_task = asyncio.create_task(
        service.cancel_descendants(SpawnId("p1")),
    )
    await first_cancel_entered.wait()
    deadline_task = asyncio.create_task(coordinator.handle_timeout())
    explicitly_cancelled, decision = await asyncio.gather(
        explicit_cancel_task,
        deadline_task,
    )

    assert explicitly_cancelled in ({"p2"}, set())
    child = spawn_store.get_spawn(tmp_path, SpawnId("p2"))
    assert child is not None
    assert child.status == "cancelled"
    assert decision.recorded_outcome is not None
    assert decision.recorded_outcome.status == "timed_out"
    assert decision.recorded_outcome.exit_code == 1
    assert decision.recorded_outcome.error == "resident_deadline_expired"
    assert cancel_calls == [SpawnId("p1"), SpawnId("p1")]
    assert connection.fake_resident_backend.awaiting_done_values == [True, False]


@pytest.mark.asyncio
async def test_intentional_stream_close_returns_pending_resident_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, clock = _coordinator(tmp_path, monkeypatch)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    terminal = await coordinator.handle_terminal_event(_EVENT, _SUCCESS, _TERMINATE)
    assert terminal.emit_turn_boundary is True

    clock.advance(1.0)
    close_outcome = coordinator.handle_close(intentional_stop=True)

    assert close_outcome == _SUCCESS
    assert connection.fake_resident_backend.awaiting_done_values == [True]
