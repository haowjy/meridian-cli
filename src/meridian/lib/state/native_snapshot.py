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
from typing import IO, Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meridian.lib.state.history_codec import TranscriptHeader

NATIVE_SNAPSHOT_FILENAME = "native-transcript.jsonl"
SNAPSHOT_RECORD = "meridian.native.snapshot"
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


class _Paused(Exception):
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
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Snapshot records must be JSON objects")
    return cast("dict[str, object]", value)


def _frame(handle: IO[bytes], limit: int, current: Callable[[], bool] | None) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if current is not None and not current():
            raise _Paused
        chunk = handle.readline(min(_READ_CHUNK, limit + 1 - size))
        if current is not None and not current():
            raise _Paused
        if not chunk:
            if size:
                raise ValueError("Incomplete snapshot frame")
            return b""
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ValueError("Snapshot frame exceeds its byte limit")
        if chunk.endswith(b"\n"):
            return b"".join(chunks)


def snapshot_header(
    handle: IO[bytes], *, current: Callable[[], bool] | None = None
) -> SnapshotHeader:
    """Read just the bounded header; this does not validate the snapshot body."""
    line = _frame(handle, HEADER_LIMIT, current)
    if not line:
        raise ValueError("Missing snapshot header")
    _object(line)
    return SnapshotHeader.model_validate_json(line)


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
    expected: SnapshotHeader | None = None,
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
        _object(line)
        header = SnapshotHeader.model_validate_json(line)
        if expected is not None and header != expected:
            raise ValueError("Snapshot header binding does not match the selected record")
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
            record = SnapshotRecord.model_validate_json(line)
            payload = contents.add(record)
            checksum.update(line)
            yield payload
    except _Paused:
        validation.reason = "Snapshot validation paused before complete EOF"
    except (ValueError, UnicodeError) as exc:
        validation.state = "corrupt"
        validation.reason = str(exc)[:1024]
        raise
    except OSError as exc:
        validation.state = "unavailable"
        validation.reason = str(exc)[:1024]
        raise
