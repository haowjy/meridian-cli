import json
from pathlib import Path

import pytest

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.state import history as history_mod
from meridian.lib.state.history import (
    HarnessHistoryWriter,
    read_history_range,
)

_WIRE_ENVELOPE_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "history" / "wire_envelopes.jsonl"
)


def _event(index: int) -> RawHarnessEvent:
    return RawHarnessEvent(
        event_type="assistant_message",
        payload={"index": index},
        harness_id="codex",
    )


def test_writer_assigns_seq_and_last_seq(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)

    assert writer.last_seq == -1
    first = writer.write(_event(0))
    second = writer.write(_event(1))

    assert first.success is True
    assert first.seq == 0
    assert second.success is True
    assert second.seq == 1
    assert writer.last_seq == 1

    rehydrated = HarnessHistoryWriter(history_path)
    assert rehydrated.last_seq == 1
    third = rehydrated.write(_event(2))
    assert third.success is True
    assert third.seq == 2


def test_writer_atomically_updates_last_observed_event_marker(tmp_path: Path) -> None:
    from tests.support.fakes import FakeClock

    history_path = tmp_path / "history.jsonl"
    marker_path = tmp_path / "last-observed-event.json"
    clock = FakeClock()
    writer = HarnessHistoryWriter(
        history_path,
        last_observed_event_path=marker_path,
        clock=clock,
    )

    for event_type in (
        "turn/started",
        "item/started",
        "item/completed",
        "item/started",
    ):
        result = writer.write(
            RawHarnessEvent(event_type=event_type, payload={}, harness_id="codex")
        )
        assert result.success is True
        clock.advance(1.0)

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker == {
        "event_kind": "item/started",
        "timestamp": marker["timestamp"],
        "seq": 3,
        "turn_started": 1,
        "turn_completed": 0,
        "item_started": 2,
        "item_completed": 1,
    }
    assert marker["timestamp"].endswith("Z")
    assert len(history_path.read_text(encoding="utf-8").splitlines()) == 4


def test_writer_throttles_last_observed_event_marker_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support.fakes import FakeClock

    history_path = tmp_path / "history.jsonl"
    marker_path = tmp_path / "last-observed-event.json"
    clock = FakeClock()
    writer = HarnessHistoryWriter(
        history_path,
        last_observed_event_path=marker_path,
        clock=clock,
    )
    write_calls = 0
    original_atomic_write_text = history_mod.atomic_write_text

    def _counting_atomic_write_text(path: Path, content: str) -> None:
        nonlocal write_calls
        if path == marker_path:
            write_calls += 1
        original_atomic_write_text(path, content)

    monkeypatch.setattr(history_mod, "atomic_write_text", _counting_atomic_write_text)

    writer.write(RawHarnessEvent(event_type="turn/started", payload={}, harness_id="codex"))
    assert write_calls == 1

    for index in range(3):
        writer.write(
            RawHarnessEvent(
                event_type="item/started",
                payload={"index": index},
                harness_id="codex",
            )
        )
    assert write_calls == 1

    clock.advance(1.0)
    writer.write(RawHarnessEvent(event_type="item/completed", payload={}, harness_id="codex"))
    assert write_calls == 2

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["event_kind"] == "item/completed"
    assert marker["seq"] == 4


def test_writer_resume_discards_truncated_tail_before_append(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)
    writer.write(_event(0))
    complete_content = history_path.read_bytes()
    history_path.write_bytes(complete_content + b'{"seq":1,"event_type":"bad"')

    resumed = HarnessHistoryWriter(history_path)
    assert resumed.last_seq == 0
    result = resumed.write(_event(1))

    assert result.success is True
    assert result.seq == 1
    raw_lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert [json.loads(line)["seq"] for line in raw_lines] == [0, 1]
    assert all('"bad"' not in line for line in raw_lines)


def test_writer_promotes_real_wire_envelopes_losslessly(tmp_path: Path) -> None:
    fixture_records = [
        json.loads(line)
        for line in _WIRE_ENVELOPE_FIXTURE.read_text(encoding="utf-8").splitlines()
    ]
    assert {record["harness_id"] for record in fixture_records} == {
        "claude",
        "codex",
        "cursor",
        "opencode",
        "pi",
    }

    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)
    for record in fixture_records:
        writer.write(
            RawHarnessEvent(
                event_type=record["event_type"],
                harness_id=record["harness_id"],
                payload=record["payload"],
                raw_text=json.dumps(record["wire"], separators=(",", ":")),
            )
        )

    written_records = read_history_range(history_path)
    for fixture, written in zip(fixture_records, written_records, strict=True):
        wire = fixture["wire"]
        payload_key = next(
            (
                key
                for key in ("payload", "params")
                if key in wire and wire[key] == fixture["payload"]
            ),
            None,
        )
        reconstructed = dict(written.get("meta", {}))
        if payload_key is None:
            reconstructed.update(written["payload"])
        else:
            reconstructed[payload_key] = written["payload"]

        assert reconstructed == wire
        assert "raw_text" not in written
        assert "request_id" not in written
        assert "item_id" not in written
        assert "turn_id" not in written
        assert "stale_after_interrupt" not in written


def test_writer_preserves_unparseable_wire_text(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    raw_text = "not valid JSON"

    result = HarnessHistoryWriter(history_path).write(
        RawHarnessEvent(
            event_type="meridian/error",
            harness_id="pi",
            payload={"error": "invalid JSON"},
            raw_text=raw_text,
        )
    )

    assert result.success is True
    assert read_history_range(history_path)[0]["meta"] == {"raw_unparsed": raw_text}


def test_writer_preserves_non_object_wire_text(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    raw_text = '"just a string"'

    result = HarnessHistoryWriter(history_path).write(
        RawHarnessEvent(
            event_type="meridian/error",
            harness_id="cursor",
            payload={"error": "unexpected wire value"},
            raw_text=raw_text,
        )
    )

    assert result.success is True
    assert read_history_range(history_path)[0]["meta"] == {"raw_unparsed": raw_text}


def test_writer_caps_unparseable_wire_text_with_visible_marker(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    raw_text = "x" * 5000

    result = HarnessHistoryWriter(history_path).write(
        RawHarnessEvent(
            event_type="meridian/error",
            harness_id="pi",
            payload={"error": "oversized invalid JSON"},
            raw_text=raw_text,
        )
    )

    assert result.success is True
    stored = read_history_range(history_path)[0]["meta"]["raw_unparsed"]
    assert isinstance(stored, str)
    assert len(stored) == 4096
    assert stored.endswith("\n[truncated by Meridian]")
    assert raw_text.startswith(stored.removesuffix("\n[truncated by Meridian]"))


def test_reader_accepts_all_wire_metadata_shapes(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    old_record = {
        "seq": 0,
        "event_type": "thread/started",
        "harness_id": "codex",
        "payload": {"threadId": "thread-old"},
        "raw_text": '{"method":"thread/started","params":{"threadId":"thread-old"}}',
    }
    new_record = {
        "seq": 1,
        "event_type": "thread/started",
        "harness_id": "codex",
        "payload": {"threadId": "thread-new"},
        "meta": {"method": "thread/started"},
    }
    unparsed_record = {
        "seq": 2,
        "event_type": "meridian/error",
        "harness_id": "pi",
        "payload": {"error": "invalid JSON"},
        "meta": {"raw_unparsed": "not valid JSON"},
    }
    metadata_free_record = {
        "seq": 3,
        "event_type": "message",
        "harness_id": "opencode",
        "payload": {"text": "hello"},
    }
    history_path.write_text(
        "".join(
            f"{json.dumps(record)}\n"
            for record in (
                old_record,
                new_record,
                unparsed_record,
                metadata_free_record,
            )
        ),
        encoding="utf-8",
    )

    assert read_history_range(history_path) == [
        old_record,
        new_record,
        unparsed_record,
        metadata_free_record,
    ]


def test_iter_history_events_tolerates_truncated_or_corrupt_lines(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        '{"seq":0,"byte_offset":0,"event_type":"ok","harness_id":"codex","payload":{"v":1}}\n'
        '{"seq":1,"byte_offset":80,"event_type":"ok","harness_id":"codex","payload":{"v":2}}\n'
        '{"seq":2,"byte_offset":160,"event_type":"bad","harness_id":"codex","payload":',
        encoding="utf-8",
    )

    events = list(history_mod.iter_history_events(history_path))
    assert [event["seq"] for event in events] == [0, 1]
def test_write_failure_returns_error_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)

    def _fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(history_mod, "append_text_line", _fail_append)
    result = writer.write(_event(0))

    assert result.success is False
    assert result.seq == -1
    assert result.error is not None
    assert "disk full" in result.error
    assert writer.last_seq == -1
    assert history_path.exists() is False


def test_writer_adds_causal_fields_and_marks_stale_after_interrupt(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)

    writer.write(
        RawHarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="item/started",
            payload={"item_id": "item-1"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="control/interrupt/requested",
            payload={"kind": "interrupt", "status": "requested", "turn_id": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-new"},
            harness_id="codex",
        )
    )
    result = writer.write(
        RawHarnessEvent(
            event_type="item/completed",
            payload={"item_id": "item-1", "text": "late"},
            harness_id="codex",
        )
    )

    assert result.success is True
    events = read_history_range(history_path)
    assert events[-1]["seq"] == 4
    assert events[-1]["turn_id"] == "turn-old"
    assert events[-1]["item_id"] == "item-1"
    assert "request_id" not in events[-1]
    assert events[-1]["interrupt_epoch"] == 1
    assert events[-1]["stale_after_interrupt"] is True


def test_writer_rehydrates_causal_tracker_from_existing_history(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)

    writer.write(
        RawHarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="item/started",
            payload={"item_id": "item-1"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="control/interrupt/requested",
            payload={"kind": "interrupt", "status": "requested", "turn_id": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        RawHarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-new"},
            harness_id="codex",
        )
    )

    resumed = HarnessHistoryWriter(history_path)
    late = resumed.write(
        RawHarnessEvent(
            event_type="item/completed",
            payload={"item_id": "item-1"},
            harness_id="codex",
        )
    )
    assert late.success is True

    events = read_history_range(history_path)
    assert events[-1]["seq"] == 4
    assert events[-1]["turn_id"] == "turn-old"
    assert events[-1]["interrupt_epoch"] == 1
    assert events[-1]["stale_after_interrupt"] is True
