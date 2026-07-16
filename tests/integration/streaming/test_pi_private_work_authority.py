from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.state import spawn_store
from meridian.lib.streaming.drain_policy import PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_completion_profile import PiCompletionProfile
from meridian.lib.streaming.pi_drain import PiCompletionEvidence
from meridian.lib.streaming.pi_quiescence import PiQuiescenceTracker
from meridian.lib.streaming.pi_subspawn_tracker import PiSubspawnTracker
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger
from tests.support.pi import pi_event
from tests.support.resident_drain import start_row


@pytest.mark.asyncio
async def test_persisted_subspawn_liveness_returns_to_tree_authority(
    tmp_path: Path,
) -> None:
    root_id = SpawnId("p1")
    start_row(tmp_path, "p1", HarnessId.PI, None)
    start_row(tmp_path, "p2", HarnessId.CODEX, "p1")
    ledger = PiPrivateWorkLedger()
    tracker = PiSubspawnTracker.empty(ledger)
    quiescence = PiQuiescenceTracker.for_connection(
        runtime_root=tmp_path,
        spawn_id=root_id,
        is_pi_connection=True,
        session_role="spawned",
        ledger=ledger,
    )
    evidence = PiCompletionEvidence(
        runtime_root=tmp_path,
        spawn_id=root_id,
        tracker=tracker,
        quiescence_tracker=quiescence,
        notification_timeout_seconds=None,
        clock=lambda: 0.0,
    )
    phases: list[dict[str, object]] = []
    profile = PiCompletionProfile(
        runtime_root=tmp_path,
        spawn_id=root_id,
        session_role="spawned",
        child_wave_timeout_seconds=None,
        emit_phase=lambda **payload: phases.append(payload),
        send_done_nudge=None,
        evidence=evidence,
        private_work_ledger=ledger,
        stabilization_seconds=0.05,
        clock=lambda: 0.0,
    )
    evidence.bind_profile(profile)
    profile.set_policy(PiRpcQuiescenceDrainPolicy(quiescence_check=lambda: False))

    await evidence.start()
    try:
        tracker.observe(
            pi_event(
                "meridian.subspawn.start",
                {
                    "subspawn_id": "p2",
                    "wait_policy": "tracked",
                    "pgid": 4202,
                },
            )
        )
        await quiescence.mark_idle()

        active = await evidence.assess("event")
        assert tuple((item.code, item.identity) for item in active.blockers) == (
            ("active_descendant", "p2"),
        )
        profile.emit_waiting_phases_if_needed()
        waiting = [item for item in phases if item["phase"] == "waiting_for_tracked_children"]
        assert waiting[-1]["persisted_descendant_count"] == 1
        assert waiting[-1]["rowless_subspawn_count"] == 0

        spawn_store.finalize_spawn(
            tmp_path,
            SpawnId("p2"),
            "succeeded",
            0,
            origin="runner",
        )
        terminal = await evidence.assess("aux_wake")

        assert terminal.disposition == "ready"
        assert ledger.blocker_snapshot(parent_idle_epoch=0.0).rowless_subspawn_ids == ()
        assert tuple(
            (item.subspawn_id, item.process_group_id)
            for item in ledger.cleanup_handles()
        ) == (("p2", 4202),)
    finally:
        await evidence.stop()
