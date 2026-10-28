from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

import pytest

from meridian.lib.harness import claude_idle
from meridian.lib.harness.idle_types import CacheTtl


def _assistant_row(*, one_hour: int = 0, five_minutes: int = 0) -> bytes:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": one_hour,
                        "ephemeral_5m_input_tokens": five_minutes,
                    }
                }
            },
        }
    ).encode()


def _transcript_path(tmp_path: Path, *, session_id: str = "session-id") -> tuple[Path, Path]:
    native_store = tmp_path / "recorded-native-store"
    native_store.mkdir()
    return native_store, native_store / f"{session_id}.jsonl"


def _transcript(*rows: bytes, session_id: str = "session-id") -> bytes:
    return b"\n".join([json.dumps({"sessionId": session_id}).encode(), *rows, b""])


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_assistant_row(one_hour=12), 3600),
        (_assistant_row(five_minutes=12), 300),
        (json.dumps({"type": "user", "message": {"content": "hello"}}).encode(), None),
    ],
)
def test_detect_ttl_from_transcript_fixtures(
    tmp_path: Path,
    row: bytes,
    expected: int | None,
) -> None:
    native_store, transcript = _transcript_path(tmp_path)
    transcript.write_bytes(_transcript(row))

    assert claude_idle.detect_ttl(
        session_id="session-id",
        native_store=native_store,
        provider=None,
        env={},
    ) == (CacheTtl(expected, estimated=False) if expected is not None else None)


def test_detect_ttl_ignores_a_truncated_last_line(tmp_path: Path) -> None:
    native_store, transcript = _transcript_path(tmp_path)
    transcript.write_bytes(
        _transcript(_assistant_row(one_hour=1)).rstrip(b"\n") + b'\n{"type":"assistant","message":'
    )

    assert claude_idle.detect_ttl(
        session_id="session-id",
        native_store=native_store,
        provider=None,
        env={},
    ) == CacheTtl(3600, estimated=False)


class _CountingReader:
    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.bytes_read = 0

    def seek(self, offset: int, whence: int = 0, /) -> int:
        return self.handle.seek(offset, whence)

    def tell(self) -> int:
        return self.handle.tell()

    def read(self, size: int = -1, /) -> bytes:
        value = self.handle.read(size)
        self.bytes_read += len(value)
        return value


def test_detect_ttl_reads_only_the_tail_of_a_transcript_larger_than_four_mib(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "large.jsonl"
    transcript.write_bytes(
        b"x" * (4 * 1024 * 1024 + 1)
        + b"\n"
        + _assistant_row(one_hour=99)
        + b"\n"
        + b'{"truncated":'
    )

    with transcript.open("rb") as handle:
        counting_reader = _CountingReader(handle)
        assert claude_idle._ttl_from_stream(counting_reader) == 3600

    assert counting_reader.bytes_read <= claude_idle._TAIL_CHUNK_BYTES
