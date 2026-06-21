"""Tests that tail_events() uses binary seek/tell (byte-stable offsets)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from meridian.lib.telemetry.reader import snapshot_segment_offsets, tail_events


def _write_event(path: Path, event: str, extra: dict | None = None) -> None:
    payload: dict = {"ts": "2026-05-01T00:00:00Z", "domain": "test", "event": event}
    if extra:
        payload.update(extra)
    with path.open("ab") as fh:
        fh.write((json.dumps(payload) + "\n").encode("utf-8"))


def test_tail_events_reads_new_event(tmp_path: Path) -> None:
    """tail_events yields events written after the offset snapshot."""
    seg = tmp_path / "seg.jsonl"
    offsets = snapshot_segment_offsets(tmp_path)
    _write_event(seg, "new.event")

    event = next(tail_events(tmp_path, poll_interval=0.001, start_offsets=offsets))

    assert event["event"] == "new.event"


def test_tail_events_byte_stable_offset_across_handle_lifetimes(tmp_path: Path) -> None:
    """Offset stored after one file handle is a valid byte seek for the next.

    This is the core binary-mode correctness test: on Windows, text-mode
    seek() with a cross-handle offset is not spec-guaranteed (CRLF can shift
    byte counts).  Binary mode makes the offset unambiguously byte-based.

    The second event is written after the generator has closed the first file
    handle, forcing a new open+seek cycle.  If the stored offset were wrong
    (char-count vs byte-count), the multibyte content would be garbled.
    """
    seg = tmp_path / "seg.jsonl"
    offsets = snapshot_segment_offsets(tmp_path)
    gen = tail_events(tmp_path, poll_interval=0.001, start_offsets=offsets)

    _write_event(seg, "e1", {"msg": "€uro sign"})
    results = [next(gen)]
    _write_event(seg, "e2", {"msg": "naïve"})
    results.append(next(gen))

    assert results[0]["event"] == "e1"
    assert results[0]["msg"] == "€uro sign"
    assert results[1]["event"] == "e2"
    assert results[1]["msg"] == "naïve"


def test_tail_events_picks_up_new_segment(tmp_path: Path) -> None:
    """Events in a segment that didn't exist at tail start are yielded."""
    offsets = snapshot_segment_offsets(tmp_path)
    gen = tail_events(tmp_path, poll_interval=0.001, start_offsets=offsets)
    seg = tmp_path / "new_seg.jsonl"
    _write_event(seg, "fresh.event")

    event = next(gen)

    assert event["event"] == "fresh.event"


def test_tail_events_domain_filter_with_binary_mode(tmp_path: Path) -> None:
    """domain filter is applied correctly when reading in binary mode."""
    seg = tmp_path / "seg.jsonl"
    offsets = snapshot_segment_offsets(tmp_path)
    gen = tail_events(tmp_path, domain="wanted", poll_interval=0.001, start_offsets=offsets)

    _write_event(seg, "skip.event")
    wanted = {
        "ts": "2026-05-01T00:00:00Z",
        "domain": "wanted",
        "event": "keep.event",
    }
    with seg.open("ab") as fh:
        fh.write((json.dumps(wanted) + "\n").encode("utf-8"))

    event = next(gen)

    assert event["event"] == "keep.event"


def test_tail_events_polls_until_new_data_arrives(tmp_path: Path, monkeypatch) -> None:
    """tail_events polls when data arrives after the offset snapshot."""
    seg = tmp_path / "seg.jsonl"
    offsets = snapshot_segment_offsets(tmp_path)
    poll_entered = threading.Event()
    real_sleep = time.sleep

    def sleep_with_barrier(interval: float) -> None:
        poll_entered.set()
        real_sleep(interval)

    monkeypatch.setattr("meridian.lib.telemetry.reader.time.sleep", sleep_with_barrier)

    def writer() -> None:
        assert poll_entered.wait(timeout=5.0), "tail_events never entered poll sleep"
        _write_event(seg, "polled.event")

    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    event = next(tail_events(tmp_path, poll_interval=0.001, start_offsets=offsets))
    writer_thread.join(timeout=5.0)

    assert event["event"] == "polled.event"
