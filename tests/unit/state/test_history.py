import json
from pathlib import Path

import pytest

from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.state import history as history_mod
from meridian.lib.state.history import (
    HarnessHistoryWriter,
    read_history_range,
)


def _event(index: int) -> HarnessEvent:
    return HarnessEvent(
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
        HarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="item/started",
            payload={"item_id": "item-1"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="control/interrupt/requested",
            payload={"kind": "interrupt", "status": "requested", "turn_id": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-new"},
            harness_id="codex",
        )
    )
    result = writer.write(
        HarnessEvent(
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
    assert events[-1]["request_id"] is None
    assert events[-1]["interrupt_epoch"] == 1
    assert events[-1]["stale_after_interrupt"] is True


def test_writer_rehydrates_causal_tracker_from_existing_history(tmp_path: Path) -> None:
    history_path = tmp_path / "history.jsonl"
    writer = HarnessHistoryWriter(history_path)

    writer.write(
        HarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="item/started",
            payload={"item_id": "item-1"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="control/interrupt/requested",
            payload={"kind": "interrupt", "status": "requested", "turn_id": "turn-old"},
            harness_id="codex",
        )
    )
    writer.write(
        HarnessEvent(
            event_type="turn/started",
            payload={"turnId": "turn-new"},
            harness_id="codex",
        )
    )

    resumed = HarnessHistoryWriter(history_path)
    late = resumed.write(
        HarnessEvent(
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
