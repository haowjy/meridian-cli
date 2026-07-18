"""SpawnManager regressions for retained Pi completion paths."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.state import spawn_store
from tests.support.async_determinism import assert_still_pending, wait_until
from tests.support.pi import (
    FakePiConnection,
    history_has_event,
    history_has_phase,
    pi_event,
    pi_process_exit_event,
    read_history,
    start_pi_manager,
    write_json,
    write_pi_bash_record,
)
from tests.support.resident_drain import start_row


class _OpenPiConnection(FakePiConnection):
    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_spawn_manager_derives_direct_followup_transitions_from_pi_events(
    tmp_path: Path,
) -> None:
    spawn_id = SpawnId("p-direct-followup")
    child_id = SpawnId("p-direct-followup-child")
    followup_ready = asyncio.Event()

    class _FollowupConnection(FakePiConnection):
        async def events(self):  # type: ignore[no-untyped-def]
            yield pi_event("agent_end")
            await followup_ready.wait()
            yield pi_event(
                "message_start",
                {
                    "role": "custom",
                    "customType": "meridian-spawn-watch",
                    "details": {"ids": [str(child_id)]},
                },
            )
            yield pi_event("agent_end")
            await asyncio.Event().wait()

    start_row(tmp_path, str(child_id), HarnessId.CODEX, str(spawn_id))
    manager = await start_pi_manager(
        tmp_path,
        _FollowupConnection([]),
        spawn_id=spawn_id,
    )
    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await wait_until(
            lambda: history_has_phase(tmp_path, spawn_id, "waiting_for_tracked_children"),
            description="Pi waiting for persisted child",
        )
        await assert_still_pending(completion)

        spawn_store.finalize_spawn(
            tmp_path,
            child_id,
            "succeeded",
            0,
            origin="runner",
        )
        write_json(
            tmp_path / "pi-bash" / str(spawn_id) / "last-notification.json",
            {"ts_epoch_secs": time.time(), "notified_spawn_ids": [str(child_id)]},
        )
        done, _ = await asyncio.wait({completion}, timeout=0.3)
        assert completion not in done

        followup_ready.set()

        outcome = await asyncio.wait_for(completion, timeout=2.0)
        assert outcome is not None
        assert outcome.status == "succeeded"
        event_types = [event["event_type"] for event in read_history(tmp_path, spawn_id)]
        assert event_types.count("agent_end") == 2
        first_idle = event_types.index("agent_end")
        followup_start = event_types.index("message_start")
        second_idle = event_types.index("agent_end", first_idle + 1)
        assert first_idle < followup_start < second_idle
        assert not any(
            event_type.startswith(("meridian.notification.", "meridian.subspawn."))
            for event_type in event_types
        )
    finally:
        followup_ready.set()
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_child_wave_timeout_publishes_before_descendant_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("p-retained-timeout")
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _GatedCleanupService:
        async def cancel_descendants(self, target_spawn_id: SpawnId) -> set[str]:
            assert target_spawn_id == spawn_id
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()
            return {"p-retained-timeout-child"}

    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.build_spawn_application_service_from_roots",
        lambda project_root, runtime_root: _GatedCleanupService(),
    )
    start_row(
        tmp_path,
        "p-retained-timeout-child",
        HarnessId.CODEX,
        str(spawn_id),
    )
    manager = await start_pi_manager(
        tmp_path,
        _OpenPiConnection([pi_event("agent_end")]),
        spawn_id=spawn_id,
        child_wave_timeout_seconds=0.01,
    )
    completion = asyncio.create_task(manager.wait_for_completion(spawn_id))

    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
        outcome = await asyncio.wait_for(completion, timeout=0.1)

        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_child_wave_timeout"
        assert not cleanup_finished.is_set()
    finally:
        allow_cleanup.set()
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_process_exit_classifies_private_bash_as_tracked_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_id = SpawnId("p-retained-bash-exit")
    cleanup_calls: list[SpawnId] = []

    class _RecordingCleanupService:
        async def cancel_descendants(self, target_spawn_id: SpawnId) -> set[str]:
            cleanup_calls.append(target_spawn_id)
            return set()

    monkeypatch.setattr(
        "meridian.lib.bootstrap.services.build_spawn_application_service_from_roots",
        lambda project_root, runtime_root: _RecordingCleanupService(),
    )
    write_pi_bash_record(tmp_path, spawn_id)
    manager = await start_pi_manager(
        tmp_path,
        FakePiConnection([pi_process_exit_event(143)]),
        spawn_id=spawn_id,
    )

    try:
        outcome = await asyncio.wait_for(manager.wait_for_completion(spawn_id), timeout=2.0)
        await wait_until(
            lambda: cleanup_calls == [spawn_id],
            description="post-publication tracked-work cancellation",
        )

        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.error == "pi_process_exited_with_tracked_children"
        assert history_has_event(tmp_path, spawn_id, "meridian/error/connectionClosed")
    finally:
        await manager.stop_spawn(spawn_id)
