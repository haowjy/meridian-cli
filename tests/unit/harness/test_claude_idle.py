from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import claude_idle
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.claude_sessions import project_slug


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
    home = tmp_path / "home"
    cwd = tmp_path / "project.with.dot"
    transcript = home / ".claude" / "projects" / project_slug(cwd) / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    return cwd, transcript


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
    cwd, transcript = _transcript_path(tmp_path)
    transcript.write_bytes(row + b"\n")

    assert (
        claude_idle.detect_ttl(
            "session-id",
            cwd,
            env={"HOME": str(tmp_path / "home")},
        )
        == expected
    )


def test_detect_ttl_ignores_a_truncated_last_line(tmp_path: Path) -> None:
    cwd, transcript = _transcript_path(tmp_path)
    transcript.write_bytes(_assistant_row(one_hour=1) + b'\n{"type":"assistant","message":')

    assert (
        claude_idle.detect_ttl(
            "session-id",
            cwd,
            env={"HOME": str(tmp_path / "home")},
        )
        == 3600
    )


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

    assert transcript.stat().st_size > 4 * 1024 * 1024
    assert counting_reader.bytes_read <= claude_idle._TAIL_CHUNK_BYTES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("TRUE", True), ("yes", True), ("on", True), ("0", False), ("", False)],
)
def test_claude_idle_env_facts(raw: str, expected: bool) -> None:
    assert claude_idle.idle_env_facts({"DISABLE_AUTO_COMPACT": raw}) == {
        "harness_autocompact_off": expected
    }


def test_claude_bundle_registers_idle_hooks() -> None:
    bundle = get_harness_bundle(HarnessId.CLAUDE)
    detector = bundle.detect_ttl

    assert detector is claude_idle.detect_ttl
    assert detector is not None
    assert detector(session_id="", cwd=None, provider=None, env={}) is None
    assert bundle.idle_env_facts is claude_idle.idle_env_facts
