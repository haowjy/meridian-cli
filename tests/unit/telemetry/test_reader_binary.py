"""Tests that tail_events() uses binary seek/tell (byte-stable offsets).

Generator initialization (snapshot) runs on the first next() call, so tests
write events from threads with a small delay to ensure initialization sees an
empty directory before data appears.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from meridian.lib.telemetry.reader import tail_events


def _write_event(path: Path, event: str, extra: dict | None = None) -> None:
    payload: dict = {"ts": "2026-05-01T00:00:00Z", "domain": "test", "event": event}
    if extra:
        payload.update(extra)
    with path.open("ab") as fh:
        fh.write((json.dumps(payload) + "\n").encode("utf-8"))


def test_tail_events_reads_new_event(tmp_path: Path) -> None:
    """tail_events yields events written after initialization."""
    seg = tmp_path / "seg.jsonl"
    gen = tail_events(tmp_path, poll_interval=0.001)

    # Delay lets the generator take its snapshot of an empty directory before
    # the file appears, so the event is not included in the initial offset.
    def writer() -> None:
        time.sleep(0.05)
        _write_event(seg, "new.event")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    event = next(gen)
    t.join(timeout=5.0)

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
    gen = tail_events(tmp_path, poll_interval=0.001)
    results: list[dict] = []

    def writer() -> None:
        time.sleep(0.05)  # let generator snapshot empty dir
        _write_event(seg, "e1", {"msg": "€uro sign"})  # 3-byte € char
        time.sleep(0.05)  # let generator process e1, close handle, store offset
        _write_event(seg, "e2", {"msg": "naïve"})  # 2-byte ï char

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    results.append(next(gen))  # e1
    results.append(next(gen))  # e2 — read via new handle seeked to stored offset
    t.join(timeout=5.0)

    assert results[0]["event"] == "e1"
    assert results[0]["msg"] == "€uro sign"
    assert results[1]["event"] == "e2"
    assert results[1]["msg"] == "naïve"


def test_tail_events_picks_up_new_segment(tmp_path: Path) -> None:
    """Events in a segment that didn't exist at tail start are yielded."""
    gen = tail_events(tmp_path, poll_interval=0.001)
    seg = tmp_path / "new_seg.jsonl"

    def writer() -> None:
        time.sleep(0.05)
        _write_event(seg, "fresh.event")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    event = next(gen)
    t.join(timeout=5.0)

    assert event["event"] == "fresh.event"


def test_tail_events_domain_filter_with_binary_mode(tmp_path: Path) -> None:
    """domain filter is applied correctly when reading in binary mode."""
    seg = tmp_path / "seg.jsonl"
    gen = tail_events(tmp_path, domain="wanted", poll_interval=0.001)

    def writer() -> None:
        time.sleep(0.05)
        _write_event(seg, "skip.event")  # domain="test", filtered out
        wanted = {
            "ts": "2026-05-01T00:00:00Z",
            "domain": "wanted",
            "event": "keep.event",
        }
        with seg.open("ab") as fh:
            fh.write((json.dumps(wanted) + "\n").encode("utf-8"))

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    event = next(gen)
    t.join(timeout=5.0)

    assert event["event"] == "keep.event"
