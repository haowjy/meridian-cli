"""Purpose-aware native reference selection over journal-only authority."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.lib.ops import reference
from meridian.lib.state import session_authority as authority
from meridian.lib.state import session_store


def _key() -> authority.NativeSessionKey:
    return authority.NativeSessionKey(
        harness="pi", store="/synthetic/native", native_session_id="conversation"
    )


def _pinned_journal(root: Path) -> authority.QualifiedLocalFile:
    begin = authority.BeginEventV4(
        run_id="run",
        attempt_id="attempt",
        transport_scope_id="transport",
        harness="pi",
        store="/synthetic/native",
        operation="fresh",
        attempt_number=1,
    )
    fact = authority.BoundaryFactV4(
        run_id="run",
        attempt_id="attempt",
        boundary="entry",
        key=_key(),
        evidence=authority.BoundaryEvidence(
            transport_scope_id="transport",
            order=1,
            correlation="entry",
            selection=authority.CreatedSelection(creation_request="fresh"),
        ),
        file=authority.QualifiedLocalFile(
            kind="local_file",
            path="/synthetic/native/conversation.jsonl",
            store_object={"device": 1, "inode": 10},
            file_object={"device": 1, "inode": 11},
            rule="pi-session-file:v1",
        ),
    )
    builder = authority._JournalBuilder()
    authority.fold_row(builder, begin)
    transition = authority.plan_attempt(builder.attempt_view(), builder.identity(), fact)
    if isinstance(transition, authority.NeedChat):
        transition = authority.plan_attempt(
            builder.attempt_view(), builder.identity(), fact, assigned_chat="c1"
        )
    assert isinstance(transition, authority.AttemptTransition)
    row = transition.row
    journal = root / "sessions.jsonl"
    journal.write_text(
        f"{begin.model_dump_json()}\n{row.model_dump_json()}\n", encoding="utf-8"
    )
    return fact.file


@pytest.mark.parametrize("purpose", ["resume", "fork", "read", "context", "capture"])
def test_each_native_use_purpose_returns_same_recorded_source(tmp_path: Path, purpose: str) -> None:
    locator = _pinned_journal(tmp_path)

    resolved = asyncio.run(
        reference.resolve_native_reference(tmp_path, "c1", purpose=purpose)  # type: ignore[arg-type]
    )

    assert isinstance(resolved, reference.AuthorizedNativeTarget)
    assert resolved.purpose == purpose
    assert resolved.source.key == _key()
    assert resolved.source.locator == locator
    assert resolved.source.ref.chat_id == "c1"
    assert resolved.source.ref.binding_event_id == authority.boundary_digest_v4(
        authority.BoundaryFactV4(
            run_id="run",
            attempt_id="attempt",
            boundary="entry",
            key=_key(),
            evidence=authority.BoundaryEvidence(
                transport_scope_id="transport",
                order=1,
                correlation="entry",
                selection=authority.CreatedSelection(creation_request="fresh"),
            ),
            file=locator,
        )
    )


def test_inspect_and_unavailable_refs_never_become_authorized_targets(tmp_path: Path) -> None:
    inspect = asyncio.run(reference.resolve_native_reference(tmp_path, "c9", purpose="inspect"))
    assert isinstance(inspect, reference.NativeInspection)
    assert inspect.binding == authority.UnavailableBinding("c9", "unknown_ref")

    unavailable = asyncio.run(reference.resolve_native_reference(tmp_path, "c9", purpose="read"))
    assert unavailable == reference.NativeUnavailable("c9", "read", "unknown_ref")


@pytest.mark.parametrize(
    "corruption",
    [
        "corrupt_prefix",
        "complete_malformed",
        "interior_corrupt",
        "unsupported",
        "duplicate_boundary",
        "torn_tail",
    ],
)
@pytest.mark.parametrize("purpose", ["read", "inspect"])
def test_corrupt_journal_fails_whole_native_snapshot_closed(
    tmp_path: Path, corruption: str, purpose: str
) -> None:
    _pinned_journal(tmp_path)
    journal = tmp_path / "sessions.jsonl"
    rows = journal.read_bytes().splitlines(keepends=True)
    pinned = b"".join(rows)
    invalid_row = b'{"event":"native_attempt","v":4,"action":"future"}\n'
    if corruption == "corrupt_prefix":
        raw = b'{"event":"native_attempt","v":5,"action":"begin"}\n' + pinned
    elif corruption == "complete_malformed":
        raw = pinned + invalid_row
    elif corruption == "interior_corrupt":
        raw = rows[0] + b'{"event":\n' + b"".join(rows[1:])
    elif corruption == "unsupported":
        raw = pinned + b'{"event":"native_attempt","v":5,"action":"begin"}\n'
    elif corruption == "duplicate_boundary":
        raw = pinned + rows[-1]
    else:
        raw = pinned + b'{"event":"native_attempt"'
    journal.write_bytes(raw)

    result = asyncio.run(
        reference.resolve_native_reference(tmp_path, "c1", purpose=purpose)  # type: ignore[arg-type]
    )

    assert journal.read_bytes() == raw
    if purpose == "inspect":
        assert result == reference.NativeInspection(
            "c1", authority.UnavailableBinding("c1", "authority_invalid")
        )
    else:
        assert result == reference.NativeUnavailable("c1", "read", "authority_invalid")


def test_v3_key_remains_occupied_but_cannot_authorize_reference_read(tmp_path: Path) -> None:
    from tests.support.attempt_owner import begin, key, observe, receipt

    begin(tmp_path, "legacy", "attempt")
    accepted = observe(tmp_path, receipt("legacy", "attempt", "entry", key("/native/old")))
    journal = tmp_path / "sessions.jsonl"
    before = journal.read_bytes()
    status = session_store.get_native_binding(tmp_path, str(accepted.chat_id))
    assert isinstance(status, authority.UnavailableBinding)
    assert status.reason == "locator_unrecorded"
    assert session_store.get_native_session_key(tmp_path, str(accepted.chat_id)) == key(
        "/native/old"
    )
    assert journal.read_bytes() == before
    assert session_store.reserve_chat_id(tmp_path) == "c2"
    result = asyncio.run(
        reference.resolve_native_reference(tmp_path, str(accepted.chat_id), purpose="resume")
    )
    assert result == reference.NativeUnavailable(
        str(accepted.chat_id), "resume", "locator_unrecorded", key("/native/old")
    )
