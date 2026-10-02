"""Disk-to-message admission and completion contracts with actual Pi collaborators."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.pi_private_state import BashEvidenceFile
from meridian.lib.streaming.disk_watcher import PiDiskWatcher
from tests.support.pi import PiDrainScenario, pi_event


def write_private(root: Path, name: str, value: object) -> None:
    path = root / "pi-bash" / "p1" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")


def bash_file(*, status: str = "exited", consumed: float | None = None) -> dict[str, object]:
    return {
        "v": 1,
        "spawn_id": "p1",
        "updated_at_ms": 1,
        "records": {
            "b1": {
                "bash_id": "b1",
                "status": status,
                "is_tracked": True,
                "is_background": True,
                "notification_consumed_at_ms": consumed,
                "command": "echo result",
                "cwd": "/tmp",
                "pid": None,
                "exit_code": 0,
                "started_at_ms": 0.0,
                "ended_at_ms": 1.0,
                "log_path": "unused",
                "stdout_log_path": "unused",
                "stderr_log_path": "unused",
                "log_bytes": 0,
                "timeout_min": 1.0,
                "originating_bash_id": None,
            }
        },
    }


async def scenario(root: Path, monkeypatch: pytest.MonkeyPatch) -> PiDrainScenario:
    started = await PiDrainScenario.start(root, monkeypatch, patch_clock=False)
    core = started.coordinator._coordinator
    core._clock = started.clock.monotonic
    started.coordinator._profile._clock = started.clock.monotonic
    started.coordinator._evidence._refresh._clock = started.clock.monotonic
    await started.idle()
    for _ in range(100):
        if started.coordinator._evidence._refresh._snapshot is not None:
            break
        await asyncio.sleep(0.025)
    return started


async def settle(started: PiDrainScenario):  # type: ignore[no-untyped-def]
    for _ in range(80):
        await asyncio.sleep(0.025)
        await started.coordinator.handle_aux_wake()
        started.clock.advance(0.05)
        decision = await started.coordinator.handle_timeout()
        if decision.recorded_outcome:
            return decision.recorded_outcome
    return None


@pytest.mark.asyncio
async def test_terminal_result_remains_owed_until_specific_message_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", bash_file())
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        assert await settle(started) is None
        # Queue admission alone cannot beat the public active/message events.
        write_private(
            tmp_path,
            "delivery-receipts.json",
            {
                "v": 1,
                "spawn_id": "p1",
                "messages": {"specific": ["b1"]},
            },
        )
        assert await settle(started) is None
        await started.coordinator.observe_event(pi_event("agent_start"), "turn_active")
        assert await settle(started) is None  # unrelated activity is not the causal receipt
        event = RawHarnessEvent(
            harness_id="pi",
            event_type="message_start",
            payload={
                "message": {
                    "role": "custom",
                    "customType": "meridian-spawn-watch",
                    "details": {"delivery_id": "specific", "work_ids": ["b1"]},
                },
            },
        )
        await started.coordinator.observe_event(event, None)
        assert await settle(started) is None  # exact receipt still fences the active turn
        await started.idle()
        await started.terminal()
        outcome = await settle(started)
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_explicit_bash_consumption_and_old_marker_need_no_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", bash_file(consumed=123.0))
    write_private(tmp_path, "last-notification.json", {"ts_epoch_secs": 10**12})
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        outcome = await settle(started)
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_child_result_is_owed_without_origin_log_or_bash_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state import spawn_store

    started = await scenario(tmp_path, monkeypatch)
    started.row("p2", parent_id="p1")
    spawn_store.finalize_spawn(tmp_path, SpawnId("p2"), "succeeded", 0, origin="runner")
    try:
        await started.terminal()
        assert await settle(started) is None
        write_private(
            tmp_path,
            "observed-spawns.json",
            {
                "v": 1,
                "spawn_id": "p1",
                "observed_spawn_ids": ["p2"],
                "waiting_spawn_ids": [],
                "wait_reservations": {},
            },
        )
        outcome = await settle(started)
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_unreadable_deadline_is_anchored_and_delivery_has_bounded_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", {"records": []})
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        started.done()
        await started.coordinator.handle_timeout()
        initial = started.coordinator._coordinator.deadline_monotonic
        started.clock.advance(299)
        await started.coordinator.observe_event(pi_event("message_update"), None)
        assert started.coordinator._coordinator.deadline_monotonic == initial
        started.clock.advance(2)
        outcome = (await started.coordinator.handle_timeout()).recorded_outcome
        assert outcome is not None and (outcome.error or "").startswith("pi_evidence_unreadable")
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_unadmitted_result_fails_with_diagnostic_after_single_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", bash_file())
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        started.clock.advance(301)
        outcome = (await started.coordinator.handle_timeout()).recorded_outcome
        assert outcome is not None and outcome.error == "pi_delivery_unresolved"
    finally:
        await started.stop()


@pytest.mark.parametrize(
    "value",
    [
        {"v": 1, "spawn_id": "p1", "updated_at_ms": 1, "records": []},
        {"v": 1, "spawn_id": "p1", "updated_at_ms": True, "records": {}},
        {"v": 1, "spawn_id": "p1", "updated_at_ms": float("inf"), "records": {}},
        {"v": True, "spawn_id": "p1", "updated_at_ms": 1, "records": {}},
        {"v": 1, "spawn_id": "p1", "updated_at_ms": 1, "records": {"b1": {"is_tracked": "yes"}}},
    ],
)
@pytest.mark.asyncio
async def test_present_malformed_private_evidence_is_unknown(tmp_path: Path, value: object) -> None:
    write_private(tmp_path, "bash-records.json", value)
    watcher = PiDiskWatcher(tmp_path, SpawnId("p1"))
    await watcher.start()
    try:
        assert watcher.evidence_failure() is not None
    finally:
        await watcher.stop()


def test_valid_canonical_bash_file() -> None:
    value = bash_file()
    assert BashEvidenceFile.model_validate(value).records["b1"].status == "exited"


@pytest.mark.asyncio
async def test_receipt_without_public_observation_fails_bounded_with_original_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(
        tmp_path,
        "delivery-receipts.json",
        {
            "v": 1,
            "spawn_id": "p1",
            "messages": {"crash-window": ["b1"]},
        },
    )
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        started.clock.advance(301)
        outcome = (await started.coordinator.handle_timeout()).recorded_outcome
        assert outcome is not None
        assert "pi_delivery_event_unobserved: crash-window" in (outcome.error or "")
        assert not (tmp_path / "pi-bash" / "p1" / "delivery-observations.json").exists()
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_consumed_admission_survives_cold_coordinator_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", bash_file())
    write_private(
        tmp_path,
        "delivery-receipts.json",
        {
            "v": 1,
            "spawn_id": "p1",
            "messages": {"prior-admission": ["b1"]},
        },
    )
    write_private(
        tmp_path,
        "delivery-observations.json",
        {
            "v": 1,
            "spawn_id": "p1",
            "observed_message_ids": ["prior-admission"],
        },
    )
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        outcome = await settle(started)
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_foreground_ownership_loss_is_unknown_until_explicit_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = bash_file(status="running")
    value["records"]["b1"].update(is_background=False, execution_error="ownership_lost")  # type: ignore[index]
    write_private(tmp_path, "bash-records.json", value)
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        assert (
            started.coordinator._coordinator.state.assessment.failure.code
            == "pi_bash_execution_unresolved"
        )
        value["records"]["b1"]["is_tracked"] = False  # type: ignore[index]
        write_private(tmp_path, "bash-records.json", value)
        outcome = await settle(started)
        assert outcome is not None and outcome.status == "succeeded"
    finally:
        await started.stop()


@pytest.mark.asyncio
async def test_delivery_window_keeps_its_anchor_without_failing_a_healthy_active_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_private(tmp_path, "bash-records.json", bash_file())
    started = await scenario(tmp_path, monkeypatch)
    try:
        await started.terminal()
        await started.coordinator.observe_event(pi_event("agent_start"), "turn_active")
        started.clock.advance(301)
        assert (await started.coordinator.handle_timeout()).recorded_outcome is None
        await started.idle()
        outcome = (await started.coordinator.handle_timeout()).recorded_outcome
        assert outcome is not None and outcome.error == "pi_delivery_unresolved"
    finally:
        await started.stop()
