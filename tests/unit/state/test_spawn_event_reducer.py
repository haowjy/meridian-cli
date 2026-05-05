from __future__ import annotations

from typing import Any

from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.events import reduce_events


class UnknownEvent:
    id = "p-skip"
    origin = "runner"
    status = "completed"


def test_reduce_events_skips_unknown_event_shapes() -> None:
    records = reduce_events([UnknownEvent()])  # type: ignore[list-item]

    assert records == {}


def test_reduce_events_tolerates_missing_ids_and_optional_start_fields() -> None:
    malformed_start = spawn_store.SpawnStartEvent.model_construct(id="p1")
    for field in ("chat_id", "parent_id", "model", "skills", "status", "started_at"):
        object.__delattr__(malformed_start, field)

    events: list[Any] = [object(), spawn_store.SpawnStartEvent(id=""), malformed_start]

    records = reduce_events(events)

    assert set(records) == {"p1"}
    record = records["p1"]
    assert record.id == "p1"
    assert record.status == "unknown"
    assert record.chat_id is None
    assert record.skills == ()


def test_reducer_skips_all_fields_from_losing_finalize_event() -> None:
    """EARS-CR2.2: losing terminal events are true no-ops in the reducer."""
    events = [
        spawn_store.SpawnStartEvent(
            id="p1",
            chat_id="c1",
            model="m1",
            agent="a1",
            harness="h1",
            status="running",
            prompt="hello",
        ),
        spawn_store.SpawnFinalizeEvent(
            id="p1",
            status="succeeded",
            exit_code=0,
            origin="runner",
            duration_secs=30.0,
            total_cost_usd=2.0,
            input_tokens=100,
            output_tokens=50,
            finished_at="2026-01-01T00:01:00Z",
        ),
        spawn_store.SpawnFinalizeEvent(
            id="p1",
            status="failed",
            exit_code=1,
            origin="launcher",
            duration_secs=99.0,
            total_cost_usd=99.0,
            input_tokens=999,
            output_tokens=999,
            cache_read_input_tokens=999,
            cache_creation_input_tokens=999,
            reasoning_tokens=999,
            cost_is_estimate=True,
            error="wrong",
            finished_at="2026-01-01T00:02:00Z",
        ),
    ]

    records = reduce_events(events)
    record = records["p1"]

    assert record.status == "succeeded"
    assert record.exit_code == 0
    assert record.terminal_origin == "runner"
    assert record.duration_secs == 30.0
    assert record.total_cost_usd == 2.0
    assert record.input_tokens == 100
    assert record.output_tokens == 50
    assert record.finished_at == "2026-01-01T00:01:00Z"
    assert record.cache_read_input_tokens is None
    assert record.cache_creation_input_tokens is None
    assert record.reasoning_tokens is None
    assert record.cost_is_estimate is False
    assert record.error is None


def test_reducer_allows_reconciler_replacement_by_authoritative() -> None:
    """Reconciler terminal replaced by later authoritative is still allowed."""
    events = [
        spawn_store.SpawnStartEvent(
            id="p1",
            chat_id="c1",
            model="m1",
            agent="a1",
            harness="h1",
            status="running",
            prompt="hello",
        ),
        spawn_store.SpawnFinalizeEvent(
            id="p1",
            status="failed",
            exit_code=1,
            origin="reconciler",
            error="orphan_run",
            finished_at="2026-01-01T00:01:00Z",
        ),
        spawn_store.SpawnFinalizeEvent(
            id="p1",
            status="succeeded",
            exit_code=0,
            origin="runner",
            duration_secs=30.0,
            total_cost_usd=2.0,
            finished_at="2026-01-01T00:02:00Z",
        ),
    ]

    records = reduce_events(events)
    record = records["p1"]

    assert record.status == "succeeded"
    assert record.exit_code == 0
    assert record.terminal_origin == "runner"
    assert record.duration_secs == 30.0
    assert record.total_cost_usd == 2.0
    assert record.finished_at == "2026-01-01T00:02:00Z"
    assert record.error is None
