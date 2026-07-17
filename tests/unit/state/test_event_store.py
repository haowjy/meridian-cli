from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from meridian.lib.state.atomic import append_text_line
from meridian.lib.state.event_store import append_event, read_events

_TAIL_SCAN_CHUNK_SIZE = 8192


class _Event(BaseModel):
    id: int
    kind: str


def _legacy_inplace_tail_repair(data_path: Path) -> None:
    """Pre-fix repair: truncate the existing inode in place."""

    with data_path.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        if end == 0:
            return
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return

        cursor = end
        truncate_at = 0
        while cursor > 0:
            chunk_start = max(0, cursor - _TAIL_SCAN_CHUNK_SIZE)
            handle.seek(chunk_start)
            chunk = handle.read(cursor - chunk_start)
            newline_index = chunk.rfind(b"\n")
            if newline_index >= 0:
                truncate_at = chunk_start + newline_index + 1
                break
            cursor = chunk_start

        handle.truncate(truncate_at)
        handle.flush()
        os.fsync(handle.fileno())


def test_append_event_repairs_truncated_trailing_line(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    data_path.write_bytes(b'{"id":1,"kind":"start"}\n{"id":2,"kind":"torn"')

    append_event(data_path, lock_path, _Event(id=3, kind="stop"))

    contents = data_path.read_bytes()
    assert contents == b'{"id":1,"kind":"start"}\n{"id":3,"kind":"stop"}\n'
    assert read_events(data_path, _Event.model_validate) == [
        _Event(id=1, kind="start"),
        _Event(id=3, kind="stop"),
    ]
    assert contents.endswith(b"\n")


def test_append_event_preserves_complete_row_missing_trailing_newline(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    data_path.write_bytes(b'{"id":1,"kind":"complete"}')

    append_event(data_path, lock_path, _Event(id=2, kind="new"))

    assert read_events(data_path, _Event.model_validate) == [
        _Event(id=1, kind="complete"),
        _Event(id=2, kind="new"),
    ]


def test_repair_crash_before_replace_preserves_original_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    original = b'{"id":1,"kind":"complete"}'
    data_path.write_bytes(original)

    import meridian.lib.platform.atomic as platform_atomic

    def _abort_before_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        raise OSError("simulated crash before inode replace")

    monkeypatch.setattr(platform_atomic.os, "replace", _abort_before_replace)

    with pytest.raises(OSError, match="simulated crash"):
        append_event(data_path, lock_path, _Event(id=2, kind="new"))

    assert data_path.read_bytes() == original
    assert read_events(data_path, _Event.model_validate) == [_Event(id=1, kind="complete")]


def test_legacy_inplace_repair_can_fabricate_hybrid_event(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    prefix_line = '{"id":1,"kind":"start"}\n'
    torn_line = '{"id":2,"kind":"to'
    data_path.write_text(prefix_line + torn_line, encoding="utf-8")

    reader_ready = threading.Event()
    repair_allowed = threading.Event()
    parsed_ids: list[int] = []

    def reader() -> None:
        with data_path.open("r", encoding="utf-8") as handle:
            assert handle.readline() == prefix_line
            reader_ready.set()
            assert repair_allowed.wait(timeout=5)
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                if isinstance(payload.get("id"), int):
                    parsed_ids.append(payload["id"])

    thread = threading.Thread(target=reader)
    thread.start()
    assert reader_ready.wait(timeout=5)

    _legacy_inplace_tail_repair(data_path)
    append_text_line(data_path, '{"id":99,"kind":"stop"}\n')
    repair_allowed.set()
    thread.join(timeout=5)

    assert 2 in parsed_ids


def test_append_event_repair_does_not_fabricate_hybrid_event(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    prefix_line = '{"id":1,"kind":"start"}\n'
    torn_line = '{"id":2,"kind":"to'
    data_path.write_text(prefix_line + torn_line, encoding="utf-8")

    reader_ready = threading.Event()
    repair_allowed = threading.Event()
    parsed_ids: list[int] = []

    def reader() -> None:
        with data_path.open("r", encoding="utf-8") as handle:
            assert handle.readline() == prefix_line
            reader_ready.set()
            assert repair_allowed.wait(timeout=5)
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload.get("id"), int):
                    parsed_ids.append(payload["id"])

    thread = threading.Thread(target=reader)
    thread.start()
    assert reader_ready.wait(timeout=5)

    append_event(data_path, lock_path, _Event(id=99, kind="stop"))
    repair_allowed.set()
    thread.join(timeout=5)

    assert 2 not in parsed_ids
    assert read_events(data_path, _Event.model_validate) == [
        _Event(id=1, kind="start"),
        _Event(id=99, kind="stop"),
    ]
