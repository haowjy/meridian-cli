"""Sealed native storage must not leak control records or accept a valid prefix."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.harness.transcript import iter_transcript_events


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _snapshot(path: Path, raw: tuple[str, ...] = ()) -> bytes:
    header = {
        "record": "meridian.native.snapshot",
        "version": 1,
        "transcript": {
            "record": "meridian.transcript",
            "version": 2,
            "history_id": str(uuid4()),
            "origin": {
                "project_id": "original",
                "spawn_id": "p1",
                "chat_id": "c1",
                "owner_chat_id": None,
                "parent_spawn_id": None,
            },
            "kind": "primary",
            "created_at": "2026-09-15T00:00:00Z",
            "relationships": {
                "parent_history_id": None,
                "owner_history_id": None,
                "forked_from_history_id": None,
            },
        },
        "session_instance_id": "generation-1",
        "harness": "pi",
        "native_session_id": "native-1",
        "dialect": "pi.session.v3",
        "scope": "append-order journal",
        "observed_from": "2026-09-15T01:00:00Z",
    }
    data = _canonical(header)
    for ordinal, item in enumerate(raw):
        data += _canonical(
            {
                "record": "meridian.native.event",
                "source": "native-1",
                "ordinal": ordinal,
                "raw": item,
            }
        )
    seal = {
        "record": "meridian.native.seal",
        "count": len(raw),
        "observed_until": "2026-09-15T01:00:01Z",
        "sources": [
            {
                "source": "native-1",
                "sha256": hashlib.sha256("".join(raw).encode()).hexdigest(),
                "records": len(raw),
            }
        ],
    }
    seal["sha256"] = hashlib.sha256(data + _canonical(seal)).hexdigest()
    data += _canonical(seal)
    path.write_bytes(data)
    return data


def test_snapshot_valid_prefix_is_not_successful_empty(tmp_path: Path) -> None:
    path = tmp_path / "native-transcript.jsonl"
    data = _snapshot(path)
    path.write_bytes(data.splitlines(keepends=True)[0])
    with pytest.raises(ValueError, match="seal"):
        list(iter_transcript_events(path))


@pytest.mark.parametrize(
    "damage", ["header", "body", "count", "revision", "trailing", "torn", "duplicate", "nan"]
)
def test_snapshot_corruption_is_not_a_successful_prefix(tmp_path: Path, damage: str) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "native-transcript.jsonl"
    data = _snapshot(path, ('{"text":"needle"}\n',))
    if damage == "header":
        data = data.replace(b"append-order journal", b"another scope")
    elif damage == "body":
        data = data.replace(b"needle", b"forged")
    elif damage == "count":
        data = data.replace(b'"count":1', b'"count":2')
    elif damage == "revision":
        data = data.replace(b'"records":1', b'"records":2')
    elif damage == "trailing":
        data += data.splitlines(keepends=True)[-1]
    elif damage == "torn":
        data = data[:-1]
    elif damage == "duplicate":
        data = data.replace(b'"version":1', b'"version":1,"version":1')
    else:
        data = data.replace(b'"ordinal":0', b'"ordinal":NaN')
    path.write_bytes(data)
    validation = TranscriptValidation()
    with pytest.raises(ValueError):
        list(iter_transcript_events(path, validation=validation))
    assert validation.state == "corrupt"
    assert validation.descriptor is None


def test_atomic_writer_preserves_raw_text_and_refuses_failed_qualification(tmp_path: Path) -> None:
    from meridian.lib.platform.atomic import atomic_replace
    from meridian.lib.state.native_snapshot import (
        SnapshotHeader,
        SnapshotObservation,
        SnapshotRecord,
        SourceRevision,
        TranscriptValidation,
        write_snapshot,
    )

    template = tmp_path / "template.jsonl"
    original = _snapshot(template)
    header = SnapshotHeader.model_validate_json(original.splitlines()[0])
    raw = '{ "text": "λ", "number": 1e2, "unknown": {"opaque": true} }\n'
    record = SnapshotRecord(source="native-1", ordinal=0, raw=raw)
    observation = SnapshotObservation(
        observed_until="2026-09-15T01:00:01Z",
        sources=(
            SourceRevision(
                source="native-1", records=1, sha256=hashlib.sha256(raw.encode()).hexdigest()
            ),
        ),
    )
    path = tmp_path / "native-transcript.jsonl"
    stream = tmp_path / "history.jsonl"
    stream.write_bytes(b"original stream with torn tail")
    with atomic_replace(path, mode="wb", encoding=None, permissions=0o600) as handle:
        descriptor = write_snapshot(handle, header, (record,), lambda: observation)
    before = path.read_bytes()
    assert json.loads(before.splitlines()[1])["raw"] == raw
    validation = TranscriptValidation()
    assert list(iter_transcript_events(path, validation=validation)) == [json.loads(raw)]
    assert validation.descriptor == descriptor

    def unfinished():
        raise ValueError("native source is unfinished")

    with (
        pytest.raises(ValueError, match="unfinished"),
        atomic_replace(path, mode="wb", encoding=None, permissions=0o600) as handle,
    ):
        write_snapshot(handle, header, (record,), unfinished)
    assert path.read_bytes() == before
    assert stream.read_bytes() == b"original stream with torn tail"
    assert list(tmp_path.glob(".native-transcript.jsonl.*.tmp")) == []


@pytest.mark.parametrize("damage", ["duplicate_marker", "torn_header", "oversized_header"])
def test_malformed_renamed_snapshot_never_downgrades_to_native_jsonl(
    tmp_path: Path, damage: str
) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "renamed.jsonl"
    data = _snapshot(path, ('{"text":"needle"}',))
    if damage == "duplicate_marker":
        data = data.replace(
            b'"record":"meridian.native.snapshot"',
            b'"record":"meridian.native.snapshot","record":"ordinary"',
            1,
        )
    elif damage == "torn_header":
        data = data.replace(b'"version":1}', b'"version":1', 1)
    else:
        data = data.replace(b"append-order journal", b"x" * 70_000)
    path.write_bytes(data)
    validation = TranscriptValidation()
    with pytest.raises(ValueError):
        list(iter_transcript_events(path, validation=validation))
    assert validation.state == "corrupt"


@pytest.mark.parametrize(
    "payload",
    [
        b'\n{"record":"meridian.native.snapshot","record":"ordinary"}\n',
        b'\n{"record":"meridian.native.snapshot",\n',
        b'\n{"recor\\u0064":"meridian.native.seal","record":"ordinary"}\n',
        b'{"text":"ok"}\n{"record":"meridian.native.snapshot","record":"ordinary"}\n',
        b'{"record":"meridian.transcript"}\n'
        b'{"record":"meridian.native.seal","record":"meridian.transcript"}\n',
    ],
    ids=[
        "later-duplicate",
        "later-torn",
        "later-escaped-duplicate",
        "after-ordinary",
        "managed-header-then-seal",
    ],
)
def test_later_reserved_marker_never_becomes_complete_transcript(
    tmp_path: Path, payload: bytes
) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    path.write_bytes(payload)
    validation = TranscriptValidation()
    with pytest.raises(ValueError):
        list(iter_transcript_events(path, validation=validation))
    assert validation.state == "corrupt"
    assert validation.descriptor is None


def test_resolved_native_source_checks_snapshot_binding(tmp_path: Path) -> None:
    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
    from meridian.lib.ops.session_transcript import SessionLogRoute, parse_session_target

    path = tmp_path / "copied-native.jsonl"
    _snapshot(path)  # Valid empty snapshot for native-1, not selected-native.
    target = SessionLogTarget(TranscriptSource.native("pi", "selected-native", path))
    with pytest.raises(ValueError, match="binding"):
        parse_session_target(
            project_root=tmp_path,
            runtime_root=None,
            target=target,
            route=SessionLogRoute("ref", "selected-native"),
        )
