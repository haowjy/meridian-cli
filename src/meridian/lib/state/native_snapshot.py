"""Streaming native snapshot codec; provider qualification is a separate contract.

Writers supply raw, qualified records and observation facts only after exhausting
that observation. This codec proves framing, binding and integrity, not native
completion. The caller owns the atomic file publication and aggregate guard.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meridian.lib.state.history_codec import TranscriptHeader

NATIVE_SNAPSHOT_FILENAME = "native-transcript.jsonl"
SNAPSHOT_RECORD = "meridian.native.snapshot"
SNAPSHOT_RECORDS = frozenset((SNAPSHOT_RECORD, "meridian.native.event", "meridian.native.seal"))
HEADER_LIMIT = 64 * 1024
FRAME_LIMIT = 64 * 1024 * 1024
_READ_CHUNK = 64 * 1024

Nonempty = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SnapshotHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    record: Literal["meridian.native.snapshot"] = SNAPSHOT_RECORD
    version: Literal[1] = 1
    transcript: TranscriptHeader
    session_instance_id: Nonempty | None
    harness: Nonempty
    native_session_id: Nonempty
    dialect: Nonempty
    scope: Nonempty
    observed_from: str

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Snapshot version must be an integer")
        return value

    @field_validator("observed_from")
    @classmethod
    def aware_time(cls, value: str) -> str:
        _time(value)
        return value


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    record: Literal["meridian.native.event"] = "meridian.native.event"
    source: Nonempty
    ordinal: Annotated[int, Field(ge=0)]
    raw: str


class SourceRevision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source: Nonempty
    sha256: Digest
    records: Annotated[int, Field(ge=0)]


class SnapshotObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    observed_until: str
    sources: tuple[SourceRevision, ...]

    @field_validator("observed_until")
    @classmethod
    def aware_time(cls, value: str) -> str:
        _time(value)
        return value


class SnapshotSeal(SnapshotObservation):
    record: Literal["meridian.native.seal"] = "meridian.native.seal"
    count: Annotated[int, Field(ge=0)]
    sha256: Digest


class SnapshotDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    header: SnapshotHeader
    seal: SnapshotSeal


@dataclass
class TranscriptValidation:
    """Storage outcome shared by readers, independent of rendering support."""

    state: Literal["complete", "partial", "corrupt", "unavailable"] = "partial"
    reason: str | None = "Transcript read has not completed"
    header: SnapshotHeader | None = None
    descriptor: SnapshotDescriptor | None = None


def _reject_unframed(validation: TranscriptValidation | None) -> None:
    reason = "Snapshot storage record outside a valid bounded header"
    if validation is not None:
        validation.state = "corrupt"
        validation.reason = reason
    raise ValueError(reason)


def reject_unframed_storage_record(
    event: dict[str, object], validation: TranscriptValidation | None = None
) -> None:
    """Keep permissive native/append readers from accepting broken storage frames."""
    marker = event.get("record")
    if isinstance(marker, str) and marker in SNAPSHOT_RECORDS:
        _reject_unframed(validation)


def reject_unframed_storage_frame(
    raw: bytes, validation: TranscriptValidation | None = None
) -> None:
    """Reject reserved markers before tolerant JSON decoding can discard them."""
    if is_snapshot_prefix(raw):
        _reject_unframed(validation)


class TranscriptReadPaused(Exception):
    pass


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("Snapshot observation time requires an explicit UTC offset")
    return stamp


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate snapshot JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError(f"Nonfinite snapshot JSON value: {value}")


def _object(raw: bytes | str) -> dict[str, object]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except RecursionError as exc:
        raise ValueError("Snapshot JSON nesting exceeds the decoder limit") from exc
    if not isinstance(value, dict):
        raise ValueError("Snapshot records must be JSON objects")
    return cast("dict[str, object]", value)


def _read_chunks(
    handle: IO[bytes],
    current: Callable[[], bool] | None,
    *,
    limit: int | None = None,
    end: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if current is not None and not current():
            raise TranscriptReadPaused
        n = _READ_CHUNK
        if limit is not None:
            n = min(n, limit + 1 - size)
            if n <= 0:
                raise ValueError("Snapshot frame exceeds its byte limit")
        if end is not None:
            remaining = end - handle.tell()
            if remaining <= 0:
                break
            n = min(n, remaining)
        chunk = handle.readline(n)
        if current is not None and not current():
            raise TranscriptReadPaused
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if limit is not None and size > limit:
            raise ValueError("Snapshot frame exceeds its byte limit")
        if chunk.endswith(b"\n"):
            break
    return b"".join(chunks)


def _frame(handle: IO[bytes], limit: int, current: Callable[[], bool] | None) -> bytes:
    line = _read_chunks(handle, current, limit=limit)
    if not line:
        return b""
    if not line.endswith(b"\n"):
        raise ValueError("Incomplete snapshot frame")
    return line


def read_jsonl_frame(
    handle: IO[bytes],
    *,
    current: Callable[[], bool] | None = None,
    end: int | None = None,
) -> bytes:
    """Read one JSONL line with cooperative budget checks and no snapshot size cap."""
    return _read_chunks(handle, current, end=end)


def is_snapshot_prefix(raw: bytes) -> bool:
    """Recognize reserved top-level markers without trusting a malformed header.

    Decode individual top-level pairs with the JSON decoder, not a last-key-wins
    dictionary. A later duplicate, torn suffix or oversized value cannot undo an
    already observed storage marker. Nested native text is not a discriminator.
    Full strict decoding and byte limits remain the reader's responsibility.
    """
    text = raw.decode("utf-8", errors="ignore").lstrip()
    if not text.startswith("{"):
        return False
    decoder = json.JSONDecoder()

    def skip_space(position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    position = 1
    try:
        while True:
            position = skip_space(position)
            key, position = decoder.raw_decode(text, position)
            position = skip_space(position)
            if text[position : position + 1] != ":":
                return False
            position += 1
            position = skip_space(position)
            value, position = decoder.raw_decode(text, position)
            if key == "record" and isinstance(value, str) and value in SNAPSHOT_RECORDS:
                return True
            position = skip_space(position)
            if text[position : position + 1] != ",":
                return False
            position += 1
    except (ValueError, RecursionError):
        return False


class _Source:
    def __init__(self) -> None:
        self.records = 0
        self.checksum = hashlib.sha256()


class _Contents:
    """Count and hash each declared raw source while retaining append order."""

    def __init__(self) -> None:
        self.count = 0
        self.sources: dict[str, _Source] = {}

    def add(self, record: SnapshotRecord) -> dict[str, object]:
        payload = _object(record.raw)
        source = self.sources.setdefault(record.source, _Source())
        if record.ordinal != source.records:
            raise ValueError("Snapshot source ordinals must be consecutive from zero")
        source.checksum.update(record.raw.encode("utf-8"))
        source.records += 1
        self.count += 1
        return payload

    def verify(self, header: SnapshotHeader, observation: SnapshotObservation) -> None:
        if _time(observation.observed_until) < _time(header.observed_from):
            raise ValueError("Snapshot observation ends before it starts")
        seen: set[str] = set()
        for source in observation.sources:
            if source.source in seen:
                raise ValueError("Duplicate snapshot source revision")
            seen.add(source.source)
            retained = self.sources.get(source.source, _Source())
            if source.records != retained.records or source.sha256 != retained.checksum.hexdigest():
                raise ValueError("Snapshot source revision does not match retained records")
        if not seen or not self.sources.keys() <= seen:
            raise ValueError("Snapshot observation must cover every retained source")


def write_snapshot(
    handle: IO[bytes],
    header: SnapshotHeader,
    records: Iterable[SnapshotRecord],
    finish: Callable[[], SnapshotObservation],
) -> SnapshotDescriptor:
    """Write one document into the caller's atomic stage, sealing only after finish.

    ``finish`` must qualify the exhausted provider observation, including source
    consistency. Exceptions propagate so the atomic publisher cannot commit.
    Raw JSON text is retained unchanged inside the storage envelope.
    """
    line = _canonical(header.model_dump(mode="json"))
    if len(line) > HEADER_LIMIT:
        raise ValueError("Snapshot header exceeds its byte limit")
    handle.write(line)
    checksum = hashlib.sha256(line)
    contents = _Contents()
    for record in records:
        contents.add(record)
        line = _canonical(record.model_dump(mode="json"))
        if len(line) > FRAME_LIMIT:
            raise ValueError("Snapshot frame exceeds its byte limit")
        handle.write(line)
        checksum.update(line)
    observation = finish()
    contents.verify(header, observation)
    metadata = {
        **observation.model_dump(mode="json"),
        "record": "meridian.native.seal",
        "count": contents.count,
    }
    checksum.update(_canonical(metadata))
    seal = SnapshotSeal.model_validate(
        {**observation.model_dump(), "count": contents.count, "sha256": checksum.hexdigest()}
    )
    line = _canonical(seal.model_dump(mode="json"))
    if len(line) > FRAME_LIMIT:
        raise ValueError("Snapshot seal exceeds its byte limit")
    handle.write(line)
    return SnapshotDescriptor(header=header, seal=seal)


def read_snapshot(
    handle: IO[bytes],
    *,
    validation: TranscriptValidation,
    current: Callable[[], bool] | None = None,
    check_header: Callable[[SnapshotHeader], None] | None = None,
) -> Iterator[dict[str, object]]:
    """Validate incrementally; yielded prefixes are unverified until final EOF.

    An early close or exhausted budget leaves ``partial``. Corruption and I/O
    failures raise and record distinct outcomes; neither authorizes source fallback.
    """
    validation.state = "partial"
    validation.reason = "Snapshot validation has not reached its final seal"
    validation.header = None
    validation.descriptor = None
    try:
        line = _frame(handle, HEADER_LIMIT, current)
        value = _object(line)
        if value.get("record") != SNAPSHOT_RECORD or type(value.get("version")) is not int:
            raise ValueError("Missing or invalid snapshot header discriminator/version")
        header = SnapshotHeader.model_validate_json(line)
        if check_header is not None:
            check_header(header)
        validation.header = header
        checksum = hashlib.sha256(line)
        contents = _Contents()
        while True:
            line = _frame(handle, FRAME_LIMIT, current)
            if not line:
                raise ValueError("Missing snapshot final seal")
            value = _object(line)
            if value.get("record") == "meridian.native.seal":
                seal = SnapshotSeal.model_validate_json(line)
                contents.verify(header, seal)
                checksum.update(
                    _canonical({key: item for key, item in value.items() if key != "sha256"})
                )
                if seal.count != contents.count or checksum.hexdigest() != seal.sha256:
                    raise ValueError("Snapshot final seal count or digest mismatch")
                if _frame(handle, FRAME_LIMIT, current):
                    raise ValueError("Trailing data after snapshot final seal")
                validation.descriptor = SnapshotDescriptor(header=header, seal=seal)
                validation.state = "complete"
                validation.reason = None
                return
            if value.get("record") != "meridian.native.event":
                raise ValueError("Missing or invalid snapshot event discriminator")
            record = SnapshotRecord.model_validate_json(line)
            payload = contents.add(record)
            checksum.update(line)
            yield payload
    except TranscriptReadPaused:
        validation.reason = "Snapshot validation paused before complete EOF"
    except (ValueError, UnicodeError) as exc:
        validation.state = "corrupt"
        validation.reason = str(exc)[:1024]
        raise
    except OSError as exc:
        validation.state = "unavailable"
        validation.reason = str(exc)[:1024]
        raise


def complete_published_snapshot(
    path: Path,
    *,
    history_id: UUID | None = None,
    harness: str | None = None,
    native_session_id: str | None = None,
) -> TranscriptValidation:
    """Validate an already published snapshot; missing is unavailable, not overwrite permission."""
    validation = TranscriptValidation()
    if not path.is_file():
        validation.state = "unavailable"
        validation.reason = "Native snapshot is missing"
        return validation

    def check_header(header: SnapshotHeader) -> None:
        if history_id is not None and header.transcript.history_id != history_id:
            raise ValueError("Snapshot history binding does not match the selected record")
        if harness is not None and header.harness != harness:
            raise ValueError("Snapshot harness binding does not match the selected record")
        if native_session_id is not None and header.native_session_id != native_session_id:
            raise ValueError("Snapshot native binding does not match the selected session")

    try:
        with path.open("rb") as handle:
            for _ in read_snapshot(handle, validation=validation, check_header=check_header):
                pass
    except (ValueError, OSError, UnicodeError):
        if validation.state not in {"corrupt", "unavailable"}:
            validation.state = "corrupt"
            validation.reason = validation.reason or "Published native snapshot is invalid"
    return validation
