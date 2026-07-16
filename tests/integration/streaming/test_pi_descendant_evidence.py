"""Characterize Pi's reconciled-descendant and private-work authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.streaming import descendant_evidence as descendant_evidence_module
from meridian.lib.streaming import pi_drain as pi_drain_module
from meridian.lib.streaming.drain_coordinator import DrainTerminalDecision
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from tests.support.async_determinism import FakeClock
from tests.support.pi import FakePiConnection, pi_event
from tests.support.resident_drain import start_row

_ROOT_ID = SpawnId("p1")
_AGENT_END = pi_event("agent_end", {})
_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_TERMINATE = DrainAction(terminate=True, emit_turn_boundary=False)


@dataclass
class _StartedPi:
    coordinator: PiDrainCoordinator
    clock: FakeClock
    phases: list[dict[str, object]]
    cleanup_reasons: list[str]


async def _start_pi(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _StartedPi:
    clock = FakeClock(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", clock.monotonic)
    phases: list[dict[str, object]] = []
    cleanup_reasons: list[str] = []
    connection = FakePiConnection([])
    await connection.start(
        ConnectionConfig(
            spawn_id=_ROOT_ID,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=runtime_root,
            env_overrides={},
            pi_session_role="spawned",
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )

    def _emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
        assert session_role == "spawned"
        phases.append({"phase": phase, **payload})

    async def _terminate_children(_tracker: object, reason: str) -> None:
        cleanup_reasons.append(reason)

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=runtime_root,
        spawn_id=_ROOT_ID,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=None,
        emit_phase=_emit_phase,
        terminate_children=_terminate_children,
    )
    await coordinator.start()
    coordinator.set_policy(
        PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent)
    )
    return _StartedPi(
        coordinator=coordinator,
        clock=clock,
        phases=phases,
        cleanup_reasons=cleanup_reasons,
    )


async def _assess_terminal(started: _StartedPi) -> DrainTerminalDecision:
    await started.coordinator.observe_event(_AGENT_END, "idle")
    return await started.coordinator.handle_terminal_event(
        _AGENT_END,
        _SUCCESS,
        _TERMINATE,
    )


@pytest.mark.asyncio
async def test_pi_tree_authority_blocks_active_direct_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        decision = await _assess_terminal(started)

        assert decision.recorded_outcome is None
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_blocks_live_grandchild_beneath_terminal_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        terminal = await _assess_terminal(started)
        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert terminal.recorded_outcome is None
        assert stabilized.recorded_outcome is None
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_polls_until_live_grandchild_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await _assess_terminal(started)
        spawn_store.finalize_spawn(
            tmp_path,
            SpawnId("p3"),
            "succeeded",
            0,
            origin="runner",
        )
        started.clock.advance(0.25)

        ready = await started.coordinator.handle_timeout()
        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert ready.recorded_outcome is None
        assert stabilized.recorded_outcome == _SUCCESS
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_polls_until_finalizing_child_gets_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.mark_finalizing(tmp_path, SpawnId("p2"))
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await _assess_terminal(started)
        (tmp_path / "spawns" / "p2" / "report.md").write_text(
            "# Report\n\nChild completed.\n",
            encoding="utf-8",
        )
        started.clock.advance(0.25)

        ready = await started.coordinator.handle_timeout()
        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert ready.recorded_outcome is None
        assert stabilized.recorded_outcome == _SUCCESS
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_polls_until_store_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    started = await _start_pi(tmp_path, monkeypatch)
    store_available = False
    list_spawns = descendant_evidence_module.spawn_store.list_spawns

    def _sometimes_list_spawns(runtime_root: Path) -> object:
        if not store_available:
            raise OSError("tree store temporarily unavailable")
        return list_spawns(runtime_root)

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _sometimes_list_spawns,
    )
    try:
        await _assess_terminal(started)
        store_available = True
        started.clock.advance(0.25)

        ready = await started.coordinator.handle_timeout()
        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert ready.recorded_outcome is None
        assert stabilized.recorded_outcome == _SUCCESS
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_only_descendant_triggers_process_exit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await _assess_terminal(started)

        exited = await started.coordinator.handle_stream_exit(None)

        assert exited.recorded_outcome is not None
        assert exited.recorded_outcome.error == "pi_process_exited_with_tracked_children"
        assert started.cleanup_reasons == ["pi_process_exit_with_tracked_children"]
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_still_blocks_on_tracked_bash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    bash_state = tmp_path / "pi-bash" / "p1" / "bash-records.json"
    bash_state.parent.mkdir(parents=True)
    bash_state.write_text(
        json.dumps(
            {
                "records": {
                    "b1": {
                        "bash_id": "b1",
                        "is_tracked": True,
                        "is_background": True,
                        "status": "running",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await _assess_terminal(started)
        started.clock.advance(0.05)

        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome is None
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_still_blocks_on_pending_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await started.coordinator.observe_event(
            pi_event("meridian.notification.queued", {"notification_id": "n1"}),
            None,
        )
        await _assess_terminal(started)
        started.clock.advance(0.05)

        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome is None
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_still_blocks_on_rowless_internal_subspawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    started = await _start_pi(tmp_path, monkeypatch)
    try:
        await started.coordinator.observe_event(
            pi_event(
                "meridian.subspawn.start",
                {"subspawn_id": "pi-internal-1", "wait_policy": "tracked"},
            ),
            None,
        )
        await _assess_terminal(started)
        started.clock.advance(0.05)

        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome is None
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_tree_authority_blocks_on_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    started = await _start_pi(tmp_path, monkeypatch)

    def _fail_list_spawns(_runtime_root: Path) -> object:
        raise OSError("tree store unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _fail_list_spawns,
    )
    try:
        terminal = await _assess_terminal(started)
        assert terminal.recorded_outcome is None

        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome is None
    finally:
        await started.coordinator.stop()
