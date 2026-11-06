from __future__ import annotations

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
