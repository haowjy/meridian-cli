"""Claude-specific idle policy observations."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Protocol, cast

from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.harness.claude_sessions import resolve_claude_native_session_file
from meridian.lib.harness.idle_types import CacheTtl

_TAIL_CHUNK_BYTES = 64 * 1024


class _SeekableBinaryReader(Protocol):
    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def tell(self) -> int: ...

    def read(self, size: int = -1, /) -> bytes: ...


def _reverse_lines(handle: _SeekableBinaryReader) -> Iterator[bytes]:
    """Yield a binary stream's lines newest-first without reading its whole tail."""

    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    remainder = b""
    while position:
        read_size = min(position, _TAIL_CHUNK_BYTES)
        position -= read_size
        handle.seek(position)
        block = handle.read(read_size) + remainder
        lines = block.split(b"\n")
        remainder = lines[0]
        yield from reversed(lines[1:])
    if remainder:
        yield remainder


def _positive_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value > 0


def _ttl_from_stream(handle: _SeekableBinaryReader) -> int | None:
    for raw_line in _reverse_lines(handle):
        if not raw_line.strip():
            continue
        try:
            raw_payload = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw_payload, dict):
            continue
        payload = cast("dict[str, object]", raw_payload)
        if payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        usage = cast("dict[str, object]", message).get("usage")
        if not isinstance(usage, dict):
            continue
        cache_creation = cast("dict[str, object]", usage).get("cache_creation")
        if not isinstance(cache_creation, dict):
            continue
        cache_creation = cast("dict[str, object]", cache_creation)
        if _positive_number(cache_creation.get("ephemeral_1h_input_tokens")):
            return 3600
        if _positive_number(cache_creation.get("ephemeral_5m_input_tokens")):
            return 300
        return None
    return None


def detect_ttl(
    *,
    session_id: str,
    native_store: Path | None,
    provider: str | None,
    env: Mapping[str, str],
) -> CacheTtl | None:
    """Detect Claude's known TTL from the latest cache-writing transcript row."""

    _ = provider, env
    if native_store is None:
        return None
    try:
        transcript = resolve_claude_native_session_file(
            session_id=session_id,
            native_store=native_store,
        )
    except NativeIdentityError:
        return None
    if transcript is None:
        return None
    try:
        with transcript.open("rb") as handle:
            seconds = _ttl_from_stream(handle)
            return CacheTtl(seconds, estimated=False) if seconds is not None else None
    except OSError:
        return None


__all__ = ["detect_ttl"]
