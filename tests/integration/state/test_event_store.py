from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.event_store import append_event, read_events
from tests.conftest import posix_only


class _ReadEvent(BaseModel):
    id: int
    kind: str


class _AppendEvent(BaseModel):
    z_key: str
    a_key: str
    optional: str | None = None


def _parse_read_event(payload: dict[str, Any]) -> _ReadEvent:
    return _ReadEvent.model_validate(payload)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


def test_read_events_skips_truncated_trailing_line(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    _write_lines(
        data_path,
        [
            '{"id":1,"kind":"start"}\n',
            '{"id":2,"kind":"update"}\n',
            '{"id":3,"kind":"broken"',
        ],
    )

    rows = read_events(data_path, _parse_read_event)

    assert [row.id for row in rows] == [1, 2]


def test_read_events_skips_malformed_json_and_validation_errors(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    _write_lines(
        data_path,
        [
            '{"id":1,"kind":"start"}\n',
            "{not-valid-json}\n",
            '{"id":"bad","kind":"update"}\n',
            '{"id":2,"kind":"done"}\n',
        ],
    )

    rows = read_events(data_path, _parse_read_event)

    assert [row.id for row in rows] == [1, 2]
def test_append_event_multiple_appends_create_multiple_lines(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"

    append_event(
        data_path,
        lock_path,
        _AppendEvent(z_key="z1", a_key="a1", optional=None),
        exclude_none=True,
    )
    append_event(
        data_path,
        lock_path,
        _AppendEvent(z_key="z2", a_key="a2", optional=None),
        exclude_none=True,
    )

    lines = data_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == '{"a_key":"a1","z_key":"z1"}'
    assert lines[1] == '{"a_key":"a2","z_key":"z2"}'
def test_lock_file_is_reentrant_in_same_thread(tmp_path: Path) -> None:
    lock_path = tmp_path / "events.lock"

    with lock_file(lock_path) as outer:
        with lock_file(lock_path) as inner:
            assert inner is outer
            assert not inner.closed
        assert not outer.closed

    assert outer.closed
    with lock_file(lock_path):
        pass


def test_append_event_repairs_truncated_trailing_line(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    data_path.write_bytes(b'{"id":1,"kind":"start"}\n{"id":2,"kind":"torn"')

    append_event(data_path, lock_path, _ReadEvent(id=3, kind="stop"))

    contents = data_path.read_bytes()
    assert contents == b'{"id":1,"kind":"start"}\n{"id":3,"kind":"stop"}\n'
    assert read_events(data_path, _ReadEvent.model_validate) == [
        _ReadEvent(id=1, kind="start"),
        _ReadEvent(id=3, kind="stop"),
    ]


@posix_only
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

    append_event(data_path, lock_path, _ReadEvent(id=99, kind="stop"))
    repair_allowed.set()
    thread.join(timeout=5)

    assert 2 not in parsed_ids
    assert read_events(data_path, _ReadEvent.model_validate) == [
        _ReadEvent(id=1, kind="start"),
        _ReadEvent(id=99, kind="stop"),
    ]
