from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.event_store import read_events
from meridian.lib.state.jsonl import JsonlDecodeError, append_durable_jsonl_line, iter_jsonl
from tests.conftest import posix_only


class _ReadEvent(BaseModel):
    id: int
    kind: str


def _append_event(data_path: Path, lock_path: Path, event: BaseModel) -> None:
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    with lock_file(lock_path):
        append_durable_jsonl_line(data_path, line)


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


def test_read_events_waits_for_newline_before_accepting_valid_tail(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    _write_lines(
        data_path,
        [
            '{"id":1,"kind":"start"}\n',
            '{"id":2,"kind":"complete-but-uncommitted"}',
        ],
    )

    assert read_events(data_path, _parse_read_event) == [_ReadEvent(id=1, kind="start")]


def test_read_events_skips_and_logs_malformed_complete_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
    assert "Skipping malformed JSONL row" not in caplog.text

    with caplog.at_level(logging.DEBUG, logger="meridian.lib.state.jsonl"):
        rows = read_events(data_path, _parse_read_event)
    assert [row.id for row in rows] == [1, 2]
    assert "Skipping malformed JSONL row" in caplog.text


def test_iter_jsonl_skips_empty_rows_but_strict_mode_rejects_corruption(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "events.jsonl"
    _write_lines(data_path, ["\n", "{not-valid-json}\n"])
    rows = iter_jsonl(data_path, _parse_read_event, on_malformed="raise")

    assert next(rows) == (None, 1)
    with pytest.raises(JsonlDecodeError, match="Malformed JSONL row"):
        next(rows)


def test_append_repairs_truncated_trailing_line(tmp_path: Path) -> None:
    data_path = tmp_path / "events.jsonl"
    lock_path = tmp_path / "events.lock"
    data_path.write_bytes(b'{"id":1,"kind":"start"}\n{"id":2,"kind":"torn"')

    _append_event(data_path, lock_path, _ReadEvent(id=3, kind="stop"))

    contents = data_path.read_bytes()
    assert contents == b'{"id":1,"kind":"start"}\n{"id":3,"kind":"stop"}\n'
    assert read_events(data_path, _ReadEvent.model_validate) == [
        _ReadEvent(id=1, kind="start"),
        _ReadEvent(id=3, kind="stop"),
    ]


@posix_only
def test_append_repair_does_not_fabricate_hybrid_event(tmp_path: Path) -> None:
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

    _append_event(data_path, lock_path, _ReadEvent(id=99, kind="stop"))
    repair_allowed.set()
    thread.join(timeout=5)

    assert 2 not in parsed_ids
    assert read_events(data_path, _ReadEvent.model_validate) == [
        _ReadEvent(id=1, kind="start"),
        _ReadEvent(id=99, kind="stop"),
    ]
