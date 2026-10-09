"""Claude-specific idle policy observations."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Protocol, cast

from meridian.lib.harness.claude_sessions import (
    candidate_claude_project_dirs,
    resolve_claude_config_root,
)

_TAIL_CHUNK_BYTES = 64 * 1024
_TRUTHY = frozenset({"1", "true", "yes", "on"})


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
    session_id: str,
    cwd: str | Path | None,
    *,
    provider: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Detect Claude's cache TTL from the latest cache-writing transcript row."""

    _ = provider
    normalized_session_id = session_id.strip()
    if (
        not normalized_session_id
        or Path(normalized_session_id).name != normalized_session_id
        or ".." in normalized_session_id
        or cwd is None
    ):
        return None
    project_root = Path(cwd)
    process_env = os.environ if env is None else env
    config_root = resolve_claude_config_root(process_env, project_root)
    candidates = candidate_claude_project_dirs(project_root, config_root)
    matches = [
        directory / f"{normalized_session_id}.jsonl"
        for directory in candidates
        if (directory / f"{normalized_session_id}.jsonl").is_file()
    ]
    if len(matches) != 1:
        return None
    try:
        with matches[0].open("rb") as handle:
            return _ttl_from_stream(handle)
    except OSError:
        return None


def autocompact_off(env: Mapping[str, str]) -> bool:
    """Return whether Claude's own automatic compaction is disabled."""

    raw = env.get("DISABLE_AUTO_COMPACT", "")
    return raw.strip().lower() in _TRUTHY


__all__ = ["autocompact_off", "detect_ttl"]
