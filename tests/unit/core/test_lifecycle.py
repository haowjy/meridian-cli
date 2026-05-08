"""Unit tests for SpawnLifecycleService and LifecycleEvent."""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog

import meridian.lib.core.telemetry as telemetry
from meridian.lib.core.lifecycle import (
    LifecycleEvent,
    SpawnLifecycleService,
    create_lifecycle_service,
    generate_event_id,
    generate_lifecycle_event_id,
    get_hook_dispatcher,
)
from meridian.lib.core.telemetry import SpawnFailureCategory
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import RuntimePaths

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingHook:
    """Captures every dispatched event for assertions."""

    def __init__(self) -> None:
        self.events: list[LifecycleEvent] = []

    def on_event(self, event: LifecycleEvent) -> None:
        self.events.append(event)


class FailingHook:
    """Always raises to verify failure isolation."""

    def on_event(self, event: LifecycleEvent) -> None:
        raise RuntimeError("deliberate hook failure")


class StoreSnapshotHook:
    """Reads store state inside hook to prove dispatch happens post-write."""

    def __init__(self, runtime_root: Path) -> None:
        self._state_root = runtime_root
        self.snapshots: list[tuple[str, str | None, str | None]] = []

    def on_event(self, event: LifecycleEvent) -> None:
        record = spawn_store.get_spawn(self._state_root, event.spawn_id)
        self.snapshots.append(
            (
                event.event_type,
                record.status if record is not None else None,
                record.terminal_origin if record is not None else None,
            )
        )


class UpdatingCreatedHook:
    """Updates spawn metadata from inside spawn.created hook."""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def on_event(self, event: LifecycleEvent) -> None:
        if event.event_type != "spawn.created":
            return
        spawn_store.update_spawn(
            self._runtime_root,
            event.spawn_id,
            desc="updated from hook",
        )


class CorrelationCapturingHook:
    """Captures structlog contextvars visible during lifecycle dispatch."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, str]] = []

    def on_event(self, event: LifecycleEvent) -> None:
        _ = event
        self.contexts.append(dict(structlog.contextvars.get_contextvars()))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_service(
    runtime_root: Path,
    hooks: list[Any] | None = None,
) -> SpawnLifecycleService:
    return SpawnLifecycleService(runtime_root, hooks=hooks)


def _start_spawn(svc: SpawnLifecycleService, **overrides: Any) -> str:
    """Start a spawn with sensible defaults."""
    defaults: dict[str, Any] = dict(
        chat_id="chat-1",
        model="claude-3-5-haiku-20241022",
        agent="coder",
        harness="claude-code",
        prompt="do the thing",
        status="queued",
    )
    defaults.update(overrides)
    return svc.start(**defaults)


# ---------------------------------------------------------------------------
# 1. Delegation to spawn_store
# ---------------------------------------------------------------------------


def test_start_creates_spawn_record(tmp_path: Path) -> None:
    """Service start() must delegate creation to spawn_store."""
    svc = _make_service(tmp_path)

    spawn_id = _start_spawn(svc)

    records = spawn_store.list_spawns(tmp_path)
    assert len(records) == 1
    assert records[0].id == spawn_id
    assert records[0].agent == "coder"
    assert records[0].harness == "claude-code"


def test_mark_running_updates_spawn_status(tmp_path: Path) -> None:
    """Service mark_running() must delegate status transition to spawn_store."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="queued")

    svc.mark_running(spawn_id)

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "running"


def test_record_exited_stores_exit_code(tmp_path: Path) -> None:
    """Service record_exited() must persist process exit code through spawn_store."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    svc.record_exited(spawn_id, exit_code=42)

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.process_exit_code == 42


def test_record_exited_emits_process_exited_telemetry_after_write(tmp_path: Path) -> None:
    """record_exited() must emit spawn.process_exited after the store write."""
    telemetry_events: list[telemetry.LifecycleEvent] = []

    class RecordingTelemetryObserver:
        def on_event(self, event: telemetry.LifecycleEvent) -> None:
            telemetry_events.append(event)

    telemetry.register_observer(RecordingTelemetryObserver())
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    svc.record_exited(spawn_id, exit_code=42)

    process_exited_events = [
        event for event in telemetry_events if event.event == "spawn.process_exited"
    ]
    assert len(process_exited_events) == 1
    event = process_exited_events[0]
    assert event.spawn_id == spawn_id
    assert event.payload == {"exit_code": 42}

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.process_exit_code == 42


def test_record_exited_reuses_pre_write_record_for_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """owner record_exited() should not reread after appending the exited event."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    real_get_spawn = spawn_store.get_spawn
    calls: list[str] = []

    def counted_get_spawn(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[1]))
        return real_get_spawn(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "get_spawn", counted_get_spawn)

    svc.record_exited(spawn_id, exit_code=42)

    assert calls == []


def test_finalize_transitions_spawn_to_terminal(tmp_path: Path) -> None:
    """Service finalize() must commit terminal status/origin through spawn_store."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.transitioned is True
    assert outcome.wrote is True
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "succeeded"
    assert record.terminal_origin == "runner"


def test_mark_finalizing_transitions_running_to_finalizing(tmp_path: Path) -> None:
    """Service mark_finalizing() must apply CAS running->finalizing via spawn_store."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    result = svc.mark_finalizing(spawn_id)

    assert result is True
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "finalizing"


def test_cancel_finalizes_with_cancelled_status(tmp_path: Path) -> None:
    """Service cancel() must route to finalize(cancelled, origin=cancel)."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    transitioned = svc.cancel(spawn_id)

    assert transitioned is True
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.terminal_origin == "cancel"


def test_failed_finalize_writes_failure_sentinel(tmp_path: Path) -> None:
    """Failed terminal transition must write a structured failure sentinel."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    outcome = svc.finalize(spawn_id, "failed", 7, origin="launcher", error="boom")

    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    assert outcome.transitioned is True
    assert outcome.wrote is True
    data = json.loads(sentinel_path.read_text(encoding="utf-8"))
    assert data["spawn_id"] == spawn_id
    assert data["exit_code"] == 7
    assert data["reason"] == "boom"
    assert data["metadata"] == {"origin": "launcher"}


def test_cancelled_finalize_does_not_write_failure_sentinel(tmp_path: Path) -> None:
    """Normal cancellation must not create a failure sentinel."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    transitioned = svc.cancel(spawn_id)

    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    assert transitioned is True
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    ("replacement_status", "replacement_origin", "replacement_exit_code"),
    [
        ("succeeded", "runner", 0),
        ("cancelled", "launcher", 130),
    ],
)
def test_authoritative_non_failed_finalize_removes_stale_failure_sentinel(
    tmp_path: Path,
    replacement_status: str,
    replacement_origin: str,
    replacement_exit_code: int,
) -> None:
    """Accepted non-failed replacement must clear stale failure.json."""
    reconciler = _make_service(tmp_path)
    authoritative = _make_service(tmp_path)
    spawn_id = _start_spawn(reconciler, status="running")

    reconciler.finalize(spawn_id, "failed", 7, origin="reconciler", error="orphan")
    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    assert sentinel_path.exists()

    outcome = authoritative.finalize(
        spawn_id,
        replacement_status,
        replacement_exit_code,
        origin=replacement_origin,
    )

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.transitioned is False
    assert outcome.wrote is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == replacement_status
    assert outcome.snapshot.terminal_origin == replacement_origin
    assert record is not None
    assert record.status == replacement_status
    assert record.terminal_origin == replacement_origin
    assert not sentinel_path.exists()


def test_failure_sentinel_write_failure_does_not_block_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel write errors are best-effort and must not block terminal state."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    def raise_write_text(*args: object, **kwargs: object) -> int:
        raise OSError("sentinel blocked")

    monkeypatch.setattr(Path, "write_text", raise_write_text)

    outcome = svc.finalize(spawn_id, "failed", 1, origin="launcher")

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.transitioned is True
    assert outcome.wrote is True
    assert record is not None
    assert record.status == "failed"


# ---------------------------------------------------------------------------
# 2. spawn.created event dispatched after start
# ---------------------------------------------------------------------------


def test_spawn_created_event_dispatched_after_start(tmp_path: Path) -> None:
    """start() must emit exactly one post-write spawn.created event."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc)

    assert len(hook.events) == 1
    event = hook.events[0]
    assert event.event_type == "spawn.created"
    assert event.spawn_id == spawn_id
    assert event.agent == "coder"
    assert event.harness == "claude-code"
    assert event.status is None
    assert event.origin is None


def test_spawn_created_event_carries_context_fields(tmp_path: Path) -> None:
    """spawn.created event must mirror persisted context fields from the created row."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])

    _start_spawn(svc, chat_id="chat-42", work_id="W1", model="claude-opus-4-5")

    event = hook.events[0]
    assert event.chat_id == "chat-42"
    assert event.work_id == "W1"
    assert event.model == "claude-opus-4-5"


def test_start_allocates_sequence_before_hook_triggered_update(tmp_path: Path) -> None:
    """Hook-triggered update_spawn events must not duplicate later start telemetry seqs."""
    telemetry_events: list[telemetry.LifecycleEvent] = []

    class RecordingTelemetryObserver:
        def on_event(self, event: telemetry.LifecycleEvent) -> None:
            telemetry_events.append(event)

    telemetry.register_observer(RecordingTelemetryObserver())
    svc = _make_service(
        tmp_path,
        hooks=[UpdatingCreatedHook(tmp_path)],
    )

    spawn_id = _start_spawn(svc, status="running")

    spawn_events = [event for event in telemetry_events if event.spawn_id == spawn_id]
    assert [(event.event, event.seq) for event in spawn_events] == [
        ("spawn.updated", 1),
        ("spawn.queued", 2),
        ("spawn.running", 3),
    ]


def test_start_reads_spawn_record_once_for_lifecycle_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() should share one post-write record across hook and telemetry payloads."""
    hook = RecordingHook()
    real_get_spawn = spawn_store.get_spawn
    calls: list[str] = []

    def counted_get_spawn(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[1]))
        return real_get_spawn(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "get_spawn", counted_get_spawn)
    svc = _make_service(tmp_path, hooks=[hook])

    spawn_id = _start_spawn(svc, status="running")

    assert calls == [spawn_id]
    assert [event.event_type for event in hook.events] == ["spawn.created"]


def test_bootstrap_from_disk_loads_owner_record_for_write_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background workers can load state once and avoid transition rereads."""
    starter = _make_service(tmp_path)
    spawn_id = _start_spawn(starter, status="running")
    worker = _make_service(tmp_path)

    bootstrapped = worker.bootstrap_from_disk(spawn_id)

    assert bootstrapped is not None
    assert bootstrapped.id == spawn_id

    def fail_get_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("bootstrapped owner transitions must not call get_spawn")

    monkeypatch.setattr(spawn_store, "get_spawn", fail_get_spawn)

    assert worker.mark_finalizing(spawn_id) is True
    outcome = worker.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.wrote is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"


# ---------------------------------------------------------------------------
# 3. spawn.running event dispatched after mark_running
# ---------------------------------------------------------------------------


def test_spawn_running_event_dispatched_after_mark_running(tmp_path: Path) -> None:
    """mark_running() must emit one spawn.running event after the store transition."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="queued")

    svc.mark_running(spawn_id)

    running_events = [e for e in hook.events if e.event_type == "spawn.running"]
    assert len(running_events) == 1
    event = running_events[0]
    assert event.spawn_id == spawn_id
    assert event.status is None
    assert event.origin is None


def test_mark_running_suppresses_duplicate_running_event(tmp_path: Path) -> None:
    """mark_running() must not re-emit spawn.running when row is already running."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")
    hook.events.clear()

    svc.mark_running(spawn_id)

    assert [e.event_type for e in hook.events] == []


def test_mark_running_reuses_pre_write_record_for_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mark_running() should use the store snapshot instead of get_spawn()."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="queued")
    hook.events.clear()

    real_get_spawn = spawn_store.get_spawn
    calls: list[str] = []

    def counted_get_spawn(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[1]))
        return real_get_spawn(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "get_spawn", counted_get_spawn)

    svc.mark_running(spawn_id, launch_mode="background", worker_pid=123, runner_pid=456)

    assert calls == []
    running_event = hook.events[0]
    assert running_event.event_type == "spawn.running"
    assert running_event.model == "claude-3-5-haiku-20241022"
    assert running_event.harness == "claude-code"


# ---------------------------------------------------------------------------
# 4. spawn.finalized dispatched after terminal writes
# ---------------------------------------------------------------------------


def test_spawn_finalized_dispatched_on_first_terminal_transition(tmp_path: Path) -> None:
    """finalize() must emit spawn.finalized exactly once on first terminal write."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.transitioned is True
    assert outcome.wrote is True
    finalized = [e for e in hook.events if e.event_type == "spawn.finalized"]
    assert len(finalized) == 1
    event = finalized[0]
    assert event.status == "succeeded"
    assert event.origin == "runner"
    assert event.spawn_id == spawn_id


def test_spawn_finalized_event_has_status_and_origin(tmp_path: Path) -> None:
    """spawn.finalized payload must include persisted terminal status/origin fields."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "failed", 1, origin="launcher", error="timeout")

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.status == "failed"
    assert event.origin == "launcher"


def test_finalize_uses_snapshot_without_get_spawn_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finalize() should rely on FinalizeOutcome.snapshot for events/telemetry."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")
    hook.events.clear()

    def fail_get_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("finalize() must not reread spawn records")

    monkeypatch.setattr(spawn_store, "get_spawn", fail_get_spawn)

    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.wrote is True
    assert [event.event_type for event in hook.events] == ["spawn.finalized"]


# ---------------------------------------------------------------------------
# 5. spawn.finalized NOT dispatched when finalize returns False
# ---------------------------------------------------------------------------


def test_spawn_finalized_dispatched_when_authoritative_overrides_reconciler(
    tmp_path: Path,
) -> None:
    """Authoritative finalize after reconciler terminal must emit replaced snapshot."""
    hook = RecordingHook()
    owner = _make_service(tmp_path)
    spawn_id = _start_spawn(owner, status="running")
    reconciler = _make_service(tmp_path)
    svc = _make_service(tmp_path, hooks=[hook])

    reconciler.finalize(spawn_id, "failed", 1, origin="reconciler", error="orphan")
    hook.events.clear()

    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.transitioned is False
    assert outcome.wrote is True
    finalized = [e for e in hook.events if e.event_type == "spawn.finalized"]
    assert len(finalized) == 1
    assert finalized[0].status == "succeeded"
    assert finalized[0].origin == "runner"


@pytest.mark.parametrize("replacement_origin", ["launcher", "runner"])
def test_owner_finalize_replaces_reconciler_terminal_with_authoritative_snapshot(
    tmp_path: Path,
    replacement_origin: str,
) -> None:
    """Owner finalize must use store terminal policy and refresh in-memory state."""
    hook = RecordingHook()
    owner = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(owner, status="running")
    reconciler = _make_service(tmp_path)

    reconciler.finalize(spawn_id, "failed", 9, origin="reconciler", error="orphan")
    hook.events.clear()

    outcome = owner.finalize(spawn_id, "succeeded", 0, origin=replacement_origin)

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert outcome.transitioned is False
    assert outcome.wrote is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.status == "succeeded"
    assert outcome.snapshot.terminal_origin == replacement_origin
    assert owner._record is not None
    assert owner._record.status == "succeeded"
    assert owner._record.terminal_origin == replacement_origin
    assert record is not None
    assert record.status == "succeeded"
    assert record.terminal_origin == replacement_origin
    finalized = [event for event in hook.events if event.event_type == "spawn.finalized"]
    assert len(finalized) == 1
    assert finalized[0].status == "succeeded"
    assert finalized[0].origin == replacement_origin


def test_spawn_finalized_not_dispatched_for_rejected_authoritative_loser(
    tmp_path: Path,
) -> None:
    """Rejected terminal writes must not emit lifecycle hooks."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "succeeded", 0, origin="runner")
    hook.events.clear()
    outcome = svc.finalize(spawn_id, "failed", 1, origin="launcher")

    assert outcome.transitioned is False
    assert outcome.wrote is False
    assert [event.event_type for event in hook.events] == []


# ---------------------------------------------------------------------------
# 6. Hook exceptions don't block transitions
# ---------------------------------------------------------------------------


def test_hook_exception_does_not_block_transition(tmp_path: Path) -> None:
    """Hook failures must not block store writes or downstream hooks."""
    failing = FailingHook()
    recording = RecordingHook()
    svc = _make_service(tmp_path, hooks=[failing, recording])

    spawn_id = _start_spawn(svc)

    # Transition completed despite failing hook
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None

    # Subsequent hooks still received the event
    assert len(recording.events) == 1
    assert recording.events[0].event_type == "spawn.created"


def test_hook_exception_does_not_block_finalize(tmp_path: Path) -> None:
    """Hook failures during finalize must not roll back terminal state writes."""
    failing = FailingHook()
    svc = _make_service(tmp_path, hooks=[failing])
    spawn_id = _start_spawn(svc, status="running")

    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert outcome.transitioned is True  # Store write succeeded despite hook failure
    assert outcome.wrote is True
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "succeeded"


# ---------------------------------------------------------------------------
# 7. LifecycleEvent is frozen (immutable)
# ---------------------------------------------------------------------------


def test_lifecycle_event_is_frozen(tmp_path: Path) -> None:
    """Dispatched LifecycleEvent instances must be immutable."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    _start_spawn(svc)

    event = hook.events[0]
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        event.spawn_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. Event IDs are stable for same spawn_id/event_type/sequence
# ---------------------------------------------------------------------------


def test_generate_event_id_is_deterministic() -> None:
    """generate_event_id() must be stable for identical inputs."""
    id1 = generate_event_id("p1", "spawn.created", 0)
    id2 = generate_event_id("p1", "spawn.created", 0)

    assert id1 == id2
    assert isinstance(id1, UUID)


def test_generate_event_id_differs_by_event_type() -> None:
    """Event type must participate in event-id identity."""
    created = generate_event_id("p1", "spawn.created", 0)
    running = generate_event_id("p1", "spawn.running", 0)

    assert created != running


def test_generate_event_id_preserves_legacy_spawn_namespace() -> None:
    """spawn.* IDs must keep legacy namespace for backward compatibility."""
    expected = uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "meridian:spawn:p1:spawn.created:0",
    )
    assert generate_event_id("p1", "spawn.created", 0) == expected


def test_generate_lifecycle_event_id_supports_non_spawn_events() -> None:
    """Non-spawn events should use the shared event namespace."""
    expected = uuid.uuid5(
        uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "meridian:event:w1:work.started:0",
    )
    assert generate_lifecycle_event_id("w1", "work.started", 0) == expected


# ---------------------------------------------------------------------------
# 9. Metrics may be None on spawn.finalized
# ---------------------------------------------------------------------------


def test_finalized_event_metrics_may_be_none(tmp_path: Path) -> None:
    """spawn.finalized fires even when no metrics are provided."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.duration_secs is None
    assert event.total_cost_usd is None
    assert event.input_tokens is None
    assert event.output_tokens is None


def test_finalized_event_includes_metrics_when_provided(tmp_path: Path) -> None:
    """spawn.finalized must surface metrics fields when finalize() provides them."""
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(
        spawn_id,
        "succeeded",
        0,
        origin="runner",
        duration_secs=12.5,
        total_cost_usd=0.003,
        input_tokens=1000,
        output_tokens=500,
    )

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.duration_secs == 12.5
    assert event.total_cost_usd == 0.003
    assert event.input_tokens == 1000
    assert event.output_tokens == 500


# ---------------------------------------------------------------------------
# 10. Illegal transition attempts — mark_finalizing guards
# ---------------------------------------------------------------------------


def test_mark_finalizing_returns_false_on_queued_spawn(tmp_path: Path) -> None:
    """mark_finalizing requires running status — queued spawn is silently rejected."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="queued")

    result = svc.mark_finalizing(spawn_id)

    assert result is False
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "queued"  # Status must be unchanged


def test_mark_finalizing_idempotent_second_call_returns_false(tmp_path: Path) -> None:
    """Calling mark_finalizing twice: first returns True, second returns False."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    first = svc.mark_finalizing(spawn_id)
    second = svc.mark_finalizing(spawn_id)  # Already finalizing, not running

    assert first is True
    assert second is False
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "finalizing"


def test_mark_finalizing_uses_store_snapshot_without_get_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mark_finalizing() should not use lifecycle get_spawn telemetry rereads."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    def fail_get_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("mark_finalizing() must not call get_spawn")

    monkeypatch.setattr(spawn_store, "get_spawn", fail_get_spawn)

    assert svc.mark_finalizing(spawn_id) is True


# ---------------------------------------------------------------------------
# 11. Repeated finalize calls — second always returns False
# ---------------------------------------------------------------------------


def test_second_finalize_returns_false_with_different_terminal_status(tmp_path: Path) -> None:
    """Second finalize with a *different* terminal status still returns False.

    The first writer to reach the terminal state wins; subsequent calls
    must not overwrite the authoritative outcome.
    """
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    first = svc.finalize(spawn_id, "succeeded", 0, origin="runner")
    second = svc.finalize(spawn_id, "failed", 1, origin="launcher")

    assert first.transitioned is True
    assert first.wrote is True
    assert second.transitioned is False
    assert second.wrote is False
    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "succeeded"  # First terminal status wins


# ---------------------------------------------------------------------------
# 14. Event ID stability across service instances
# ---------------------------------------------------------------------------


def test_event_id_stable_across_service_instances(tmp_path: Path) -> None:
    """Events for the same spawn_id/event_type carry identical IDs regardless
    of which service instance built the event."""
    hook1 = RecordingHook()
    hook2 = RecordingHook()

    svc1 = _make_service(tmp_path, hooks=[hook1])
    spawn_id = _start_spawn(svc1)  # spawns through svc1

    svc2 = _make_service(tmp_path, hooks=[hook2])
    svc2.mark_running(spawn_id)

    expected_created_id = generate_event_id(spawn_id, "spawn.created", 0)
    expected_running_id = generate_event_id(spawn_id, "spawn.running", 0)

    assert hook1.events[0].event_id == expected_created_id
    assert hook2.events[0].event_id == expected_running_id


# ---------------------------------------------------------------------------
# 15. Invalid transition: mark_running on a terminal spawn raises ValueError
# ---------------------------------------------------------------------------


def test_mark_running_on_terminal_spawn_raises_value_error(tmp_path: Path) -> None:
    """mark_running on a terminal spawn (succeeded → running) must raise ValueError.

    Terminal states have no allowed outbound transitions.  The store's
    _validate_transition guard raises ValueError which propagates through the
    service unchanged — the caller must handle the illegal-transition case.
    """
    owner = _make_service(tmp_path)
    spawn_id = _start_spawn(owner, status="running")
    owner.finalize(spawn_id, "succeeded", 0, origin="runner")
    svc = _make_service(tmp_path)

    # Spawn is now in terminal state: succeeded → running is forbidden
    with pytest.raises(ValueError, match="Illegal spawn transition"):
        svc.mark_running(spawn_id)


def test_owner_mark_running_on_terminal_spawn_is_silently_dropped(tmp_path: Path) -> None:
    """Owner mark_running must not overwrite terminal state when guard rejects."""
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")
    svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    svc.mark_running(spawn_id)

    record = spawn_store.get_spawn(tmp_path, spawn_id)
    assert record is not None
    assert record.status == "succeeded"


# ---------------------------------------------------------------------------
# 16. Required lifecycle-path coverage (Phase 5.1)
# ---------------------------------------------------------------------------


def test_required_path_start_and_running_dispatches_post_write(tmp_path: Path) -> None:
    """Required path: start/running events must observe post-write store snapshots."""
    recording = RecordingHook()
    snapshot = StoreSnapshotHook(tmp_path)
    svc = _make_service(tmp_path, hooks=[recording, snapshot])

    spawn_id = _start_spawn(svc, status="queued")
    svc.mark_running(spawn_id)

    assert [event.event_type for event in recording.events] == ["spawn.created", "spawn.running"]
    assert snapshot.snapshots == [
        ("spawn.created", "queued", None),
        ("spawn.running", "running", None),
    ]


def test_required_path_mark_finalizing_then_finalize(tmp_path: Path) -> None:
    """Required path: finalizing+finalize must emit one finalized event with runner origin."""
    recording = RecordingHook()
    snapshot = StoreSnapshotHook(tmp_path)
    svc = _make_service(tmp_path, hooks=[recording, snapshot])
    spawn_id = _start_spawn(svc, status="running")

    marked = svc.mark_finalizing(spawn_id)
    outcome = svc.finalize(spawn_id, "succeeded", 0, origin="runner")

    assert marked is True
    assert outcome.transitioned is True
    assert outcome.wrote is True
    assert [event.event_type for event in recording.events] == ["spawn.created", "spawn.finalized"]
    finalized_event = recording.events[-1]
    assert finalized_event.status == "succeeded"
    assert finalized_event.origin == "runner"
    assert snapshot.snapshots[-1] == ("spawn.finalized", "succeeded", "runner")


def test_required_path_cancel_origin_and_post_write_hook(tmp_path: Path) -> None:
    """Required path: cancel must finalize with cancel origin and post-write visibility."""
    recording = RecordingHook()
    snapshot = StoreSnapshotHook(tmp_path)
    svc = _make_service(tmp_path, hooks=[recording, snapshot])
    spawn_id = _start_spawn(svc, status="running")

    transitioned = svc.cancel(spawn_id)

    assert transitioned is True
    finalized_event = next(
        event for event in recording.events if event.event_type == "spawn.finalized"
    )
    assert finalized_event.status == "cancelled"
    assert finalized_event.origin == "cancel"
    assert snapshot.snapshots[-1] == ("spawn.finalized", "cancelled", "cancel")


# ---------------------------------------------------------------------------
# 17. Lifecycle factory hook wiring
# ---------------------------------------------------------------------------


def test_get_hook_dispatcher_returns_none_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOOKS_ENABLED", "false")

    dispatcher = get_hook_dispatcher(tmp_path, tmp_path / ".meridian")

    assert dispatcher is None


def test_get_hook_dispatcher_returns_dispatcher_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.lib.hooks.dispatch import HookDispatcher

    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)

    dispatcher = get_hook_dispatcher(tmp_path, tmp_path / ".meridian")

    assert isinstance(dispatcher, HookDispatcher)


def test_create_lifecycle_service_centralizes_hook_enablement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOOKS_ENABLED", "false")
    disabled_service = create_lifecycle_service(tmp_path, tmp_path / ".meridian")

    monkeypatch.setenv("MERIDIAN_HOOKS_ENABLED", "true")
    enabled_service = create_lifecycle_service(tmp_path, tmp_path / ".meridian")

    assert disabled_service._hooks == []
    assert len(enabled_service._hooks) == 1
    assert enabled_service._hooks[0].__class__.__name__ == "HookDispatcher"


def test_finalized_event_exposes_typed_outcome_category(tmp_path: Path) -> None:
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "failed", 1, origin="launch_failure", error="launch died")

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.outcome_category == SpawnFailureCategory.LAUNCH_FAILURE


def test_failure_sentinel_uses_canonical_terminal_diagnostic_shape(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running", work_id="W1")

    svc.finalize(spawn_id, "failed", 7, origin="launch_failure", error="launch died")

    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    data = json.loads(sentinel_path.read_text(encoding="utf-8"))
    assert data["category"] == "launch_failure"
    assert data["status"] == "failed"
    assert data["origin"] == "launch_failure"
    assert data["correlation"]["spawn_id"] == spawn_id
    assert data["correlation"]["work_id"] == "W1"
    assert data["correlation"]["failure_category"] == "launch_failure"


def test_lifecycle_correlation_binds_and_resets_without_cross_spawn_bleed(tmp_path: Path) -> None:
    hook_a = CorrelationCapturingHook()
    hook_b = CorrelationCapturingHook()
    svc_a = _make_service(tmp_path, hooks=[hook_a])
    svc_b = _make_service(tmp_path, hooks=[hook_b])

    spawn_a = _start_spawn(svc_a, work_id="WA")
    assert structlog.contextvars.get_contextvars() == {}

    spawn_b = _start_spawn(svc_b, work_id="WB")
    assert structlog.contextvars.get_contextvars() == {}

    assert hook_a.contexts[0]["spawn_id"] == spawn_a
    assert hook_a.contexts[0]["work_id"] == "WA"
    assert hook_b.contexts[0]["spawn_id"] == spawn_b
    assert hook_b.contexts[0]["work_id"] == "WB"
    assert hook_b.contexts[0]["spawn_id"] != hook_a.contexts[0]["spawn_id"]


def test_failed_terminal_telemetry_includes_category(tmp_path: Path) -> None:
    telemetry_events: list[telemetry.LifecycleEvent] = []

    class RecordingTelemetryObserver:
        def on_event(self, event: telemetry.LifecycleEvent) -> None:
            telemetry_events.append(event)

    telemetry.register_observer(RecordingTelemetryObserver())
    svc = _make_service(tmp_path)
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "failed", 1, origin="reconciler", error="orphaned")

    failed = next(event for event in telemetry_events if event.event == "spawn.failed")
    assert failed.payload["category"] == "reconciler_orphan"


def test_failed_terminal_uses_unknown_category_for_unclassified_launcher_failures(
    tmp_path: Path,
) -> None:
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "failed", 9, origin="launcher", error="plain launcher failure")

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.outcome_category == SpawnFailureCategory.UNKNOWN_FAILURE

    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    data = json.loads(sentinel_path.read_text(encoding="utf-8"))
    assert data["category"] == "unknown_failure"


def test_failed_terminal_does_not_infer_teardown_from_error_text(tmp_path: Path) -> None:
    hook = RecordingHook()
    svc = _make_service(tmp_path, hooks=[hook])
    spawn_id = _start_spawn(svc, status="running")

    svc.finalize(spawn_id, "failed", 5, origin="launcher", error="teardown boom")

    event = next(e for e in hook.events if e.event_type == "spawn.finalized")
    assert event.outcome_category == SpawnFailureCategory.UNKNOWN_FAILURE

    sentinel_path = RuntimePaths.from_root_dir(tmp_path).spawns_dir / spawn_id / "failure.json"
    data = json.loads(sentinel_path.read_text(encoding="utf-8"))
    assert data["category"] == "unknown_failure"
