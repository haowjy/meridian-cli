"""Small contract tests for crash-only lifecycle behavior."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from meridian.lib.core.lifecycle import (
    LifecycleEvent,
    SpawnLifecycleService,
    generate_event_id,
    generate_lifecycle_event_id,
)
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import RuntimePaths


class StoreSnapshotHook:
    """Reads persisted state during hook dispatch to prove post-write visibility."""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root
        self.snapshots: list[tuple[str, str | None, str | None]] = []

    def on_event(self, event: LifecycleEvent) -> None:
        record = spawn_store.get_spawn(self._runtime_root, event.spawn_id)
        self.snapshots.append(
            (
                event.event_type,
                None if record is None else record.status,
                None if record is None else record.terminal_origin,
            )
        )


class EventTypeHook:
    """Captures lifecycle event types for assertions."""

    def __init__(self) -> None:
        self.event_types: list[str] = []

    def on_event(self, event: LifecycleEvent) -> None:
        self.event_types.append(event.event_type)


def _make_service(
    runtime_root: Path,
    hooks: list[Any] | None = None,
) -> SpawnLifecycleService:
    return SpawnLifecycleService(runtime_root, hooks=hooks)


def _start_spawn(service: SpawnLifecycleService, **overrides: Any) -> str:
    defaults: dict[str, Any] = {
        "chat_id": "c1",
        "session_metadata": PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.4",
            agent="coder",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        "prompt": "run it",
        "status": "running",
    }
    defaults.update(overrides)
    return service.start(**defaults)


def test_generate_event_id_is_deterministic() -> None:
    assert generate_event_id("p1", "spawn.created", 0) == generate_event_id(
        "p1",
        "spawn.created",
        0,
    )


def test_generate_event_id_differs_by_event_type() -> None:
    assert generate_event_id("p1", "spawn.created", 0) != generate_event_id(
        "p1",
        "spawn.running",
        0,
    )


def test_generate_event_id_preserves_legacy_spawn_namespace() -> None:
    expected = uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "meridian:spawn:p1:spawn.created:0",
    )
    assert generate_event_id("p1", "spawn.created", 0) == expected


def test_generate_lifecycle_event_id_supports_non_spawn_events() -> None:
    expected = uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "meridian:event:w1:work.started:0",
    )
    assert generate_lifecycle_event_id("w1", "work.started", 0) == expected


def test_failure_sentinel_write_failure_does_not_block_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    spawn_id = _start_spawn(service)

    def fail_write_text(*_args: object, **_kwargs: object) -> int:
        raise OSError("sentinel blocked")

    monkeypatch.setattr(Path, "write_text", fail_write_text)

    outcome = service.finalize(spawn_id, "failed", 1, origin="launcher")

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.transitioned is True
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "failed"


def test_authoritative_non_failed_finalize_removes_stale_failure_sentinel(
    tmp_path: Path,
) -> None:
    reconciler = _make_service(tmp_path)
    authoritative = _make_service(tmp_path)
    spawn_id = _start_spawn(reconciler)

    reconciler.finalize(spawn_id, "failed", 7, origin="reconciler", error="orphan")
    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    assert sentinel_path.exists()

    outcome = authoritative.finalize(spawn_id, "succeeded", 0, origin="runner")

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.transitioned is False
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "succeeded"
    assert record.terminal_origin == "runner"
    assert not sentinel_path.exists()


def test_bootstrap_from_disk_loads_owner_record_for_write_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starter = _make_service(tmp_path)
    spawn_id = _start_spawn(starter)
    worker = _make_service(tmp_path)

    bootstrapped = worker.bootstrap_from_disk(spawn_id)

    assert bootstrapped is not None
    assert bootstrapped.id == spawn_id

    def fail_get_spawn(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("bootstrapped owner transitions must not call get_spawn")

    monkeypatch.setattr(spawn_store, "get_spawn", fail_get_spawn)

    assert worker.mark_finalizing(spawn_id) is True
    outcome = worker.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.wrote is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"


def test_finalize_dispatches_hooks_after_terminal_state_is_persisted(tmp_path: Path) -> None:
    snapshot_hook = StoreSnapshotHook(tmp_path)
    service = _make_service(tmp_path, hooks=[snapshot_hook])
    spawn_id = _start_spawn(service)

    service.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert snapshot_hook.snapshots[-1] == ("spawn.finalized", "succeeded", "runner")


def test_start_accepts_typed_metadata_and_persists_goal(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    spawn_id = service.start(
        chat_id="c1",
        session_metadata=PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.4",
            agent="coder",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        prompt="run it",
        metadata=SpawnStartMetadata(
            desc="goal metadata",
            work_id="  w-lifecycle  ",
            goal="  finish migration  ",
        ),
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.desc == "goal metadata"
    assert record.work_id == "w-lifecycle"
    assert record.goal == "finish migration"


def test_start_without_dispatch_events_defers_hook_until_announce_started(tmp_path: Path) -> None:
    hook = EventTypeHook()
    service = _make_service(tmp_path, hooks=[hook])

    spawn_id = service.start(
        chat_id="c1",
        session_metadata=PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.4",
            agent="coder",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        prompt="run it",
        status="queued",
        dispatch_events=False,
    )

    assert hook.event_types == []

    service.announce_started(spawn_id)

    assert hook.event_types == ["spawn.created"]


def test_owner_mark_running_clears_stale_runner_created_epoch_when_pid_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path)
    spawn_id = _start_spawn(service)
    service.mark_running(
        spawn_id,
        runner_pid=os.getpid(),
        runner_created_at_epoch=222.0,
    )
    initial = spawn_store.get_spawn(tmp_path, spawn_id)
    assert initial is not None
    assert initial.runner_created_at_epoch == 222.0

    monkeypatch.setattr("meridian.lib.core.lifecycle._pid_created_at_epoch", lambda _pid: None)
    service.mark_running(
        spawn_id,
        runner_pid=999997,
    )

    updated = spawn_store.get_spawn(tmp_path, spawn_id)
    assert updated is not None
    assert updated.runner_pid == 999997
    assert updated.runner_created_at_epoch is None
