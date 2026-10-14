"""Shared JSONL event store helpers for Meridian state files."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import append_durable_jsonl_line
from meridian.lib.utils.json_shapes import json_object

T = TypeVar("T")
MalformedPolicy = Literal["skip", "raise"]

logger = logging.getLogger(__name__)


class JsonlDecodeError(ValueError):
    """A complete JSONL row is corrupt or invalid for its reader."""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_event(
    data_path: Path,
    lock_path: Path,
    event: BaseModel,
    *,
    exclude_none: bool = False,
) -> None:
    payload = event.model_dump(mode="json", exclude_none=exclude_none)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    with lock_file(lock_path):
        append_durable_jsonl_line(data_path, line)


def iter_jsonl(
    path: Path,
    parse: Callable[[dict[str, object]], T | None],
    *,
    offset: int = 0,
    on_malformed: MalformedPolicy = "skip",
) -> Generator[tuple[T | None, int], None, None]:
    """Yield complete JSONL rows and their ending byte offsets.

    A final row without a newline is never committed and is left for a later
    read. Empty complete rows are ignored. Malformed complete rows are skipped
    with a warning by default, or raise when the caller must fail closed.
    """

    if not path.exists():
        return
    with path.open("rb") as handle:
        handle.seek(offset)
        while raw_line := handle.readline():
            if not raw_line.endswith(b"\n"):
                break
            end_offset = handle.tell()
            if not raw_line.strip():
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
                logger.warning(
                    "Skipping malformed JSONL row at byte %d: %s",
                    end_offset - len(raw_line),
                    path,
                )
                value = None
            yield value, end_offset


def read_events(
    data_path: Path,
    parse_event: Callable[[dict[str, object]], T | None],
) -> list[T]:
    return [row for row, _offset in iter_jsonl(data_path, parse_event) if row is not None]
