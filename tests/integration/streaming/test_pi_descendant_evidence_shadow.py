"""Characterize Pi's reconciled-descendant shadow without changing authority."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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


async def _start_pi(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> _StartedPi:
    clock = FakeClock(start=100.0)
    monkeypatch.setattr(pi_drain_module.time, "monotonic", clock.monotonic)
    caplog.set_level(logging.INFO, logger=pi_drain_module.__name__)
    phases: list[dict[str, object]] = []
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

    coordinator = PiDrainCoordinator.for_connection(
        runtime_root=runtime_root,
        spawn_id=_ROOT_ID,
        receiver=connection,
        session_role="spawned",
        notification_timeout_seconds=None,
        child_wave_timeout_seconds=None,
        emit_phase=_emit_phase,
    )
    await coordinator.start()
    coordinator.set_policy(
        PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent)
    )
    return _StartedPi(coordinator=coordinator, clock=clock, phases=phases)


def _shadow_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for record in caplog.records:
        payload = getattr(record, "descendant_evidence_shadow", None)
        if isinstance(payload, dict):
            records.append(cast("dict[str, object]", payload))
    return records


def _shadow_categories(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [record["category"] for record in _shadow_records(caplog)]


async def _assess_terminal(started: _StartedPi) -> DrainTerminalDecision:
    await started.coordinator.observe_event(_AGENT_END, "idle")
    return await started.coordinator.handle_terminal_event(
        _AGENT_END,
        _SUCCESS,
        _TERMINATE,
    )


@pytest.mark.asyncio
async def test_pi_shadow_reports_active_direct_child_in_both_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    started = await _start_pi(tmp_path, monkeypatch, caplog)
    try:
        decision = await _assess_terminal(started)

        assert decision.recorded_outcome is None
        assert _shadow_categories(caplog) == ["both"]
        assert _shadow_records(caplog)[0] == {
            "category": "both",
            "spawn_ids": ("p2",),
            "watcher_active_count": 1,
            "tree_active_count": 1,
        }
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_shadow_reports_tree_only_grandchild_but_keeps_watcher_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    start_row(tmp_path, "p3", HarnessId.CODEX, "p2")
    started = await _start_pi(tmp_path, monkeypatch, caplog)
    try:
        terminal = await _assess_terminal(started)
        assert terminal.recorded_outcome is None
        assert _shadow_categories(caplog) == ["tree-only"]

        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome == _SUCCESS
        assert _shadow_categories(caplog) == ["tree-only", "tree-only"]
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_shadow_reports_reconciled_terminal_direct_child_as_watcher_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    spawn_store.mark_finalizing(tmp_path, SpawnId("p2"))
    (tmp_path / "spawns" / "p2" / "report.md").write_text(
        "# Report\n\nChild completed.\n",
        encoding="utf-8",
    )
    started = await _start_pi(tmp_path, monkeypatch, caplog)
    try:
        decision = await _assess_terminal(started)

        assert decision.recorded_outcome is None
        assert _shadow_categories(caplog) == ["watcher-only"]
        assert _shadow_records(caplog)[0]["spawn_ids"] == ("p2",)
        assert _shadow_records(caplog)[0]["tree_active_count"] == 0
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_shadow_reports_allocation_uncertainty_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    (tmp_path / "spawns" / "p2").mkdir()
    started = await _start_pi(tmp_path, monkeypatch, caplog)
    try:
        decision = await _assess_terminal(started)

        assert decision.recorded_outcome is None
        assert _shadow_categories(caplog) == ["allocation-uncertainty"]
    finally:
        await started.coordinator.stop()


@pytest.mark.asyncio
async def test_pi_shadow_store_error_does_not_block_authoritative_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_row(tmp_path, "p1", HarnessId.PI, None)
    started = await _start_pi(tmp_path, monkeypatch, caplog)

    def _fail_list_spawns(_runtime_root: Path) -> object:
        raise OSError("shadow store unavailable")

    monkeypatch.setattr(
        descendant_evidence_module.spawn_store,
        "list_spawns",
        _fail_list_spawns,
    )
    try:
        terminal = await _assess_terminal(started)
        assert terminal.recorded_outcome is None
        assert _shadow_categories(caplog) == ["store-error"]
        assert _shadow_records(caplog)[0]["error_code"] == (
            "descendant_evidence_read_failed"
        )
        assert _shadow_records(caplog)[0]["error_detail"] == "shadow store unavailable"

        started.clock.advance(0.05)
        stabilized = await started.coordinator.handle_timeout()

        assert stabilized.recorded_outcome == _SUCCESS
        assert _shadow_categories(caplog) == ["store-error", "store-error"]
    finally:
        await started.coordinator.stop()
