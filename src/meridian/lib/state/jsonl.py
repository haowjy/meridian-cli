"""One JSONL commit rule: a row exists when its newline does.

``append_durable_jsonl_line`` accepts exactly one row and owns its terminating
newline. Callers may omit that delimiter, but embedded CR/LF characters are
rejected so one call cannot commit multiple rows accidentally.

Durable repair and append require the caller to hold the writer lock (or own a
single-writer file). Repair is a read-then-replace operation, so concurrent
writers without that exclusion can overwrite one another.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from typing import BinaryIO, Literal, TypeVar

from meridian.lib.platform.atomic import atomic_replace
from meridian.lib.state.atomic import append_text_line
from meridian.lib.utils.json_shapes import json_object

T = TypeVar("T")
MalformedPolicy = Literal["skip", "raise"]

logger = logging.getLogger(__name__)


class JsonlDecodeError(ValueError):
    """A complete JSONL row is corrupt or invalid for its reader."""


def iter_jsonl(
    path: Path,
    parse: Callable[[dict[str, object]], T | None],
    *,
    offset: int = 0,
    on_malformed: MalformedPolicy = "skip",
    raw_filter: Callable[[bytes], bool] | None = None,
) -> Generator[tuple[T | None, int], None, None]:
    """Yield newline-committed JSON object rows and their ending byte offsets."""

    if not path.exists():
        return
    with path.open("rb") as handle:
        yield from iter_jsonl_file(
            handle,
            parse,
            path=path,
            offset=offset,
            on_malformed=on_malformed,
            raw_filter=raw_filter,
        )


def iter_jsonl_file(
    handle: BinaryIO,
    parse: Callable[[dict[str, object]], T | None],
    *,
    path: Path,
    offset: int = 0,
    extent: int | None = None,
    on_malformed: MalformedPolicy = "skip",
    raw_filter: Callable[[bytes], bool] | None = None,
) -> Generator[tuple[T | None, int], None, None]:
    """Yield committed rows from one open inode, optionally bounded to an extent."""

    handle.seek(offset)
    while extent is None or handle.tell() < extent:
        remaining = -1 if extent is None else extent - handle.tell()
        raw_line = handle.readline(remaining)
        if not raw_line or not raw_line.endswith(b"\n"):
            break
        end_offset = handle.tell()
        if not raw_line.strip():
            yield None, end_offset
            continue
        if raw_filter is not None and not raw_filter(raw_line):
            yield None, end_offset
            continue
        try:
            decoded: object = json.loads(raw_line)
            payload = json_object(decoded)
            if payload is None:
                raise ValueError("JSONL row is not an object")
            value = parse(payload)
        except (UnicodeDecodeError, ValueError) as error:
            if on_malformed == "raise":
                raise JsonlDecodeError(
                    f"Malformed JSONL row at byte {end_offset - len(raw_line)}: {path}"
                ) from error
            logger.debug(
                "Skipping malformed JSONL row at byte %d: %s",
                end_offset - len(raw_line),
                path,
            )
            value = None
        yield value, end_offset


def _jsonl_tail_needs_repair(path: Path) -> bool:
    """Return True when an existing JSONL file lacks a trailing newline."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if end == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


def _repaired_jsonl_bytes(content: bytes) -> bytes | None:
    """Return repaired JSONL bytes, or None when no repair is needed."""

    if not content or content.endswith(b"\n"):
        return None

    last_newline = content.rfind(b"\n")
    if last_newline < 0:
        prefix = b""
        tail = content
    else:
        prefix = content[: last_newline + 1]
        tail = content[last_newline + 1 :]

    try:
        parsed = json.loads(tail.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        return prefix + tail + b"\n"
    return prefix


def repair_jsonl_tail(path: Path) -> None:
    """Repair a torn or delimiter-less tail before acknowledging another append."""

    if not _jsonl_tail_needs_repair(path):
        return

    content = path.read_bytes()
    repaired = _repaired_jsonl_bytes(content)
    if repaired is None:
        return

    with atomic_replace(path, mode="wb", encoding=None, permissions="preserve") as handle:
        handle.write(repaired)


def append_durable_jsonl_lines(path: Path, lines: Iterable[str]) -> None:
    """Repair a torn JSONL tail, then append rows with one write and fsync."""

    content = "".join(lines)
    if not content:
        return

    repair_jsonl_tail(path)
    append_text_line(path, content)


def append_durable_jsonl_line(path: Path, line: str) -> None:
    """Terminate one newline-free row, repair the tail, and append durably."""

    row = line[:-1] if line.endswith("\n") else line
    if "\n" in row or "\r" in row:
        raise ValueError("JSONL row must not contain embedded newlines")
    append_durable_jsonl_lines(path, (row + "\n",))


__all__ = [
    "JsonlDecodeError",
    "MalformedPolicy",
    "append_durable_jsonl_line",
    "append_durable_jsonl_lines",
    "iter_jsonl",
    "iter_jsonl_file",
    "repair_jsonl_tail",
]
