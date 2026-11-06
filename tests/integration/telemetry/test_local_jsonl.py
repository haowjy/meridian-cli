from __future__ import annotations

import json
import os

from meridian.lib.telemetry.events import TelemetryEnvelope
from meridian.lib.telemetry.local_jsonl import LocalJSONLSink
from meridian.lib.telemetry.reader import read_events


def envelope(event: str = "chat.ws.connected") -> TelemetryEnvelope:
    return TelemetryEnvelope(
        v=1,
        ts="2026-05-02T12:00:00Z",
        domain="chat",
        event=event,
        scope="chat.server.ws",
        severity="info",
        ids={"chat_id": "c1"},
    )


def test_write_batch_creates_compound_segment_with_json_lines(tmp_path, monkeypatch) -> None:
    fsync_calls = 0
    original_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    sink = LocalJSONLSink(tmp_path)
    sink.write_batch([envelope("event.one"), envelope("event.two"), envelope("event.three")])
    sink.close()

    segment = tmp_path / "telemetry" / f"cli.{os.getpid()}-0001.jsonl"
    assert segment.exists()
    lines = segment.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == [
        "event.one",
        "event.two",
        "event.three",
    ]
    assert fsync_calls == 1


def test_rotation_opens_next_segment_without_renaming_active_file(tmp_path) -> None:
    sink = LocalJSONLSink(tmp_path, max_segment_bytes=1)
    sink.write_batch([envelope()])
    first = tmp_path / "telemetry" / f"cli.{os.getpid()}-0001.jsonl"
    second = tmp_path / "telemetry" / f"cli.{os.getpid()}-0002.jsonl"

    assert first.exists()
    assert second.exists()
    payload = json.loads(first.read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "chat.ws.connected"
    sink.close()


def test_write_batch_skips_non_serializable_events(tmp_path) -> None:
    sink = LocalJSONLSink(tmp_path)
    bad = TelemetryEnvelope(
        v=1,
        ts="2026-05-02T12:00:00Z",
        domain="chat",
        event="chat.ws.connected",
        scope="chat.server.ws",
        data={"bad": object()},
    )
    sink.write_batch([bad, envelope("chat.ws.disconnected")])
    sink.close()

    segment = tmp_path / "telemetry" / f"cli.{os.getpid()}-0001.jsonl"
    lines = segment.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "chat.ws.disconnected"


def test_write_batch_repairs_valid_tail_missing_newline(tmp_path, caplog) -> None:
    sink = LocalJSONLSink(tmp_path)
    sink.write_batch([envelope("event.one")])
    segment = sink.active_path
    content = segment.read_bytes()
    assert content.endswith(b"\n")
    segment.write_bytes(content[:-1])

    batch = [envelope("event.two"), envelope("event.three"), envelope("event.four")]
    sink.write_batch(batch)
    sink.close()

    events = list(read_events(segment))
    assert len(events) == len(batch) + 1
    assert [event["event"] for event in events] == [
        "event.one",
        "event.two",
        "event.three",
        "event.four",
    ]
    assert not [
        record for record in caplog.records if "Skipping malformed JSONL row" in record.getMessage()
    ]


def test_custom_logical_owner_in_segment_filename(tmp_path) -> None:
    sink = LocalJSONLSink(tmp_path, logical_owner="p42")
    sink.write_batch([envelope()])
    sink.close()

    segment = tmp_path / "telemetry" / f"p42.{os.getpid()}-0001.jsonl"
    assert segment.exists()
    lines = segment.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
