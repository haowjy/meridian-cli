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


def test_explicit_renamed_snapshot_yields_only_native_records(tmp_path: Path) -> None:
    path = tmp_path / "renamed.jsonl"
    raw = (
        '{ "type": "message", "message": {"role":"assistant", '
        '"content":[{"type":"text", "text":"retained"}]}}\n'
    )
    _snapshot(path, (raw,))
    assert list(iter_transcript_events(path)) == [json.loads(raw)]


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


def test_empty_snapshot_requires_final_seal_and_preserves_identity(tmp_path: Path) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    _snapshot(path)
    validation = TranscriptValidation()
    assert list(iter_transcript_events(path, validation=validation)) == []
    assert validation.state == "complete"
    assert validation.descriptor is not None
    assert validation.descriptor.header.native_session_id == "native-1"
    assert validation.descriptor.seal.count == 0


def test_close_after_one_record_is_partial_not_complete(tmp_path: Path) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    _snapshot(path, ('{"text":"needle"}\n',))
    validation = TranscriptValidation()
    events = iter_transcript_events(path, validation=validation)
    assert next(events) == {"text": "needle"}
    events.close()
    assert validation.state == "partial"
    assert validation.descriptor is None


def test_large_frame_checks_budget_between_bounded_reads(tmp_path: Path) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation, read_snapshot

    path = tmp_path / "copy.jsonl"
    _snapshot(path, (json.dumps({"text": "x" * 500_000}),))
    validation = TranscriptValidation()
    with path.open("rb") as handle:
        assert (
            list(
                read_snapshot(
                    handle, validation=validation, current=lambda: handle.tell() < 100_000
                )
            )
            == []
        )
        assert 100_000 <= handle.tell() < 200_000
    assert validation.state == "partial"
    assert validation.descriptor is None


def test_binding_and_frame_limits_are_checked_before_complete(tmp_path: Path, monkeypatch) -> None:
    from meridian.lib.state import native_snapshot as codec

    path = tmp_path / "copy.jsonl"
    data = _snapshot(path, ('{"text":"needle"}',))
    expected = codec.SnapshotHeader.model_validate_json(data.splitlines()[0])
    wrong = expected.model_copy(update={"native_session_id": "other"})

    def check_header(actual):
        if actual != wrong:
            raise ValueError("wrong snapshot binding")

    with path.open("rb") as handle, pytest.raises(ValueError, match="binding"):
        list(
            codec.read_snapshot(
                handle, validation=codec.TranscriptValidation(), check_header=check_header
            )
        )
    monkeypatch.setattr(codec, "FRAME_LIMIT", 16)
    with path.open("rb") as handle, pytest.raises(ValueError, match="byte limit"):
        list(codec.read_snapshot(handle, validation=codec.TranscriptValidation()))


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


def test_shared_parse_never_falls_back_from_empty_or_budget_partial_snapshot(
    tmp_path: Path,
) -> None:
    import time

    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
    from meridian.lib.ops.session_transcript import (
        SessionLogRoute,
        TranscriptBudget,
        parse_session_target,
    )

    path = tmp_path / "copy.jsonl"
    _snapshot(path)
    fallback = tmp_path / "fallback.jsonl"
    fallback.write_text('{"type":"assistant","message":{"content":"wrong source"}}\n')
    target = SessionLogTarget(TranscriptSource("file", "native-1", "pi", "snapshot", path))
    parsed = parse_session_target(
        project_root=tmp_path,
        runtime_root=None,
        target=target,
        route=SessionLogRoute("file", str(path)),
    )
    assert parsed.entries == ()
    assert parsed.target.source.source_label == "snapshot"
    assert parsed.storage_validation is not None
    assert parsed.storage_validation.state == "complete"
    partial = parse_session_target(
        project_root=tmp_path,
        runtime_root=None,
        target=target,
        route=SessionLogRoute("file", str(path)),
        budget=TranscriptBudget(time.monotonic() - 1, 1_000_000),
    )
    assert partial.target.source.source_label == "snapshot"
    assert partial.storage_validation is not None
    assert partial.storage_validation.state == "partial"
    assert partial.read_reasons


def test_partial_sealed_prefix_is_visible_to_log_but_not_search(tmp_path: Path) -> None:
    import time

    from meridian.lib.ops.session_search import _matches_for_transcript
    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
    from meridian.lib.ops.session_transcript import (
        SessionLogRoute,
        TranscriptBudget,
        parse_session_target,
    )

    path = tmp_path / "copy.jsonl"
    raw = json.dumps({"type": "assistant", "message": {"content": "early needle"}})
    _snapshot(path, (raw, json.dumps({"padding": "x" * 10000})))
    source = TranscriptSource("file", "native-1", "claude", "snapshot", path)
    target = SessionLogTarget(source)
    parsed = parse_session_target(
        project_root=tmp_path,
        runtime_root=None,
        target=target,
        route=SessionLogRoute("file", str(path)),
        budget=TranscriptBudget(time.monotonic() + 10, 1000),
    )
    assert any("early needle" in entry.content for entry in parsed.entries)
    assert parsed.storage_validation is not None
    assert parsed.storage_validation.state == "partial"
    assert parsed.read_reasons
    assert (
        _matches_for_transcript(
            transcript=parsed,
            query="needle",
            query_lower="needle",
            corpus="file",
            chat_id="native-1",
        )
        == []
    )


def test_loose_prefix_retains_search_readiness(tmp_path: Path) -> None:
    import time

    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource
    from meridian.lib.ops.session_transcript import (
        SessionLogRoute,
        TranscriptBudget,
        parse_session_target,
    )

    path = tmp_path / "stream.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": "early needle"}})
        + "\n"
        + json.dumps({"padding": "x" * 10000})
        + "\n"
    )
    source = TranscriptSource("file", "native-1", "claude", "stream", path)
    parsed = parse_session_target(
        project_root=tmp_path,
        runtime_root=None,
        target=SessionLogTarget(source),
        route=SessionLogRoute("file", str(path)),
        budget=TranscriptBudget(time.monotonic() + 10, 1000),
    )
    assert any("early needle" in entry.content for entry in parsed.entries)
    assert parsed.search_ready
    assert parsed.target.source == source


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
    "change", ["missing_version", "bool_version", "float_version", "missing_marker"]
)
def test_wire_header_requires_explicit_exact_discriminator(tmp_path: Path, change: str) -> None:
    path = tmp_path / "native-transcript.jsonl"
    data = _snapshot(path)
    header, seal = (json.loads(line) for line in data.splitlines())
    if change == "missing_version":
        del header["version"]
    elif change == "bool_version":
        header["version"] = True
    elif change == "float_version":
        header["version"] = 1.0
    else:
        del header["record"]
    del seal["sha256"]
    seal["sha256"] = hashlib.sha256(_canonical(header) + _canonical(seal)).hexdigest()
    path.write_bytes(_canonical(header) + _canonical(seal))
    with pytest.raises(ValueError):
        list(iter_transcript_events(path))


def test_snapshot_preview_bypasses_stream_append_checkpoint(tmp_path: Path, monkeypatch) -> None:
    from meridian.lib.ops import session_preview
    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource

    path = tmp_path / "history.jsonl"  # Renamed storage still must not become an append stream.
    _snapshot(
        path,
        (
            json.dumps({"type": "session", "id": "native-1", "version": 3, "cwd": str(tmp_path)}),
            json.dumps(
                {
                    "type": "message",
                    "id": "a",
                    "parentId": None,
                    "message": {"role": "user", "content": "retained needle"},
                }
            ),
        ),
    )
    source = TranscriptSource("native_file", "native-1", "pi", "snapshot", path)
    target = SessionLogTarget(source)
    monkeypatch.setattr(session_preview, "resolve_roots_for_read", lambda _: None)
    monkeypatch.setattr(session_preview, "resolve_transcript_source", lambda **_: target)
    view = session_preview.SessionPreview(str(tmp_path)).refresh(
        session_preview.PreviewIdentity("p1", history_id="fixture"), lambda: True
    )
    assert view is not None
    assert "retained needle" in "\n".join(view.lines)
    assert view.state == "current"


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


@pytest.mark.parametrize(
    "payload",
    [
        b'\n{"record":"meridian.native.snapshot","record":"ordinary"}\n',
        b'\n{"record":"meridian.native.snapshot",\n',
        b'\n{"recor\\u0064":"meridian.native.seal","record":"ordinary"}\n',
        b'{"record":"meridian.transcript"}\n'
        b'{"record":"meridian.native.seal","record":"meridian.transcript"}\n',
    ],
    ids=[
        "later-duplicate",
        "later-torn",
        "later-escaped-duplicate",
        "managed-header-then-seal",
    ],
)
def test_managed_preview_rejects_later_reserved_markers(
    tmp_path: Path, payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.lib.ops import session_preview
    from meridian.lib.ops.session_target import SessionLogTarget, TranscriptSource

    path = tmp_path / "history.jsonl"
    path.write_bytes(payload)
    source = TranscriptSource("native_file", "p1", "pi", "owned", path)
    target = SessionLogTarget(source)
    monkeypatch.setattr(session_preview, "resolve_roots_for_read", lambda _: None)
    monkeypatch.setattr(session_preview, "resolve_transcript_source", lambda **_: target)
    view = session_preview.SessionPreview(str(tmp_path)).refresh(
        session_preview.PreviewIdentity("p1", history_id="fixture"), lambda: True
    )
    assert view is not None
    assert view.state == "unavailable"


def test_ordinary_unmarked_malformed_lines_stay_tolerant(tmp_path: Path) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    path.write_text(
        '{"type":"assistant","message":{"content":"ok"}}\n{"type":"bad",\n',
        encoding="utf-8",
    )
    validation = TranscriptValidation()
    assert list(iter_transcript_events(path, validation=validation)) == [
        {"type": "assistant", "message": {"content": "ok"}}
    ]
    assert validation.state == "complete"


def test_nested_reserved_strings_stay_readable(tmp_path: Path) -> None:
    path = tmp_path / "copy.jsonl"
    event = {
        "type": "assistant",
        "message": {
            "content": "meridian.native.snapshot",
            "record": "meridian.native.seal",
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert list(iter_transcript_events(path)) == [event]


def test_fallback_oversized_pre_marker_checks_budget_between_reads(tmp_path: Path) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    header = json.loads(_snapshot(path).splitlines()[0])
    path.write_bytes(_canonical({**header, "dialect": "x" * 2_000_000}))
    checks: list[int] = []

    def current() -> bool:
        checks.append(1)
        return len(checks) == 1

    validation = TranscriptValidation()
    events = list(iter_transcript_events(path, validation=validation, current=current))
    assert events == []
    assert validation.state == "partial"
    assert validation.descriptor is None
    assert len(checks) >= 2


def test_fallback_oversized_pre_marker_fails_closed_when_budget_allows(
    tmp_path: Path,
) -> None:
    from meridian.lib.state.native_snapshot import TranscriptValidation

    path = tmp_path / "copy.jsonl"
    header = json.loads(_snapshot(path).splitlines()[0])
    path.write_bytes(_canonical({**header, "dialect": "x" * 2_000_000}))
    validation = TranscriptValidation()
    with pytest.raises(ValueError):
        list(iter_transcript_events(path, validation=validation))
    assert validation.state == "corrupt"
    assert validation.descriptor is None
