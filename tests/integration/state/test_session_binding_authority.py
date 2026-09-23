from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.lib.state import session_store


def key(store: str, native_id: str = "native-1") -> session_store.NativeSessionKey:
    return session_store.NativeSessionKey(harness="pi", store=store, native_session_id=native_id)


def receipt(
    run_id: str,
    attempt_id: str,
    boundary: str,
    native_key: session_store.NativeSessionKey,
    *,
    order: int = 1,
    operation: str = "fresh",
    source_key: session_store.NativeSessionKey | None = None,
    scope_id: str | None = None,
) -> session_store.OwnedBoundaryReceipt:
    return session_store.OwnedBoundaryReceipt(
        run_id=run_id,
        attempt_id=attempt_id,
        boundary=boundary,
        key=native_key,
        evidence=session_store.BoundaryEvidence(
            owner_attempt_id=attempt_id,
            transport_scope_id=scope_id or f"transport:{attempt_id}",
            order=order,
            qualified=True,
            operation=operation,
            source_key=source_key,
            fresh_creation_verified=operation == "fresh",
            fork_ancestry_verified=operation == "fork",
            before_delivery=boundary == "entry",
            terminal=boundary == "exit",
        ),
    )


def begin(root: Path, run_id: str, attempt_id: str, **kwargs: object) -> None:
    kwargs.setdefault("transport_scope_id", f"transport:{attempt_id}")
    session_store.begin_native_attempt(root, run_id, attempt_id, **kwargs)


def test_native_key_is_store_qualified_and_binding_is_immutable(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    left, right = key("/native/A"), key("/native/B")
    begin(root, "run-a", "attempt-a")
    begin(root, "run-b", "attempt-b")
    left_id = session_store.accept_native_boundary(
        root, receipt("run-a", "attempt-a", "entry", left)
    ).chat_id
    right_id = session_store.accept_native_boundary(
        root, receipt("run-b", "attempt-b", "entry", right)
    ).chat_id
    assert left_id != right_id
    assert (
        session_store.accept_native_boundary(
            root, receipt("run-a", "attempt-a", "exit", left, order=2)
        ).chat_id
        == left_id
    )
    assert session_store.get_native_session_key(root, str(left_id)) == left
    with pytest.raises(ValueError, match="immutable"):
        session_store.accept_native_boundary(
            root, receipt("run-a", "attempt-a", "entry", right, order=3)
        )


def test_concurrent_duplicate_key_exits_converge_and_replay_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    native_key = key("/native/shared")
    receipts = []
    for index in range(2):
        run_id, attempt_id = f"run-{index}", f"attempt-{index}"
        begin(root, run_id, attempt_id)
        session_store.accept_native_boundary(root, receipt(run_id, attempt_id, "entry", native_key))
        receipts.append(receipt(run_id, attempt_id, "exit", native_key, order=4))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda r: session_store.accept_native_boundary(root, r), receipts))
    assert results[0].chat_id == results[1].chat_id
    before = (root / "sessions.jsonl").read_bytes()
    assert session_store.accept_native_boundary(root, receipts[0]) == results[0]
    assert (root / "sessions.jsonl").read_bytes() == before


def test_resume_source_and_attempt_ownership_are_enforced(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    pinned, wrong = key("/native/pinned"), key("/native/other")
    begin(root, "source-run", "source-attempt")
    session_store.accept_native_boundary(
        root, receipt("source-run", "source-attempt", "entry", pinned)
    )
    begin(root, "run", "attempt", operation="resume", requested_source=pinned)
    with pytest.raises(ValueError, match="pinned native source"):
        session_store.accept_native_boundary(
            root,
            receipt("run", "attempt", "entry", wrong, operation="resume", source_key=pinned),
        )
    with pytest.raises(ValueError, match="unknown or unstarted"):
        session_store.accept_native_boundary(root, receipt("run", "old-attempt", "exit", pinned))
    with pytest.raises(ValueError, match="not owned"):
        session_store.accept_native_boundary(
            root,
            receipt(
                "run",
                "attempt",
                "entry",
                pinned,
                operation="resume",
                source_key=pinned,
                scope_id="child-scope",
            ),
        )


def test_old_attempt_receipt_cannot_cross_successor_attempt_boundary(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "retrying-run", "attempt-1")
    session_store.accept_native_boundary(
        root, receipt("retrying-run", "attempt-1", "entry", key("/native/stale"))
    )
    begin(root, "retrying-run", "attempt-2")
    with pytest.raises(ValueError, match="superseded attempt"):
        session_store.accept_native_boundary(
            root, receipt("retrying-run", "attempt-1", "exit", key("/native/stale"))
        )


def test_late_exit_contradiction_is_durable_and_invalidates_only_attempt(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    a, b = key("/native/A"), key("/native/B")
    session_store.accept_native_boundary(root, receipt("run", "attempt", "entry", a))
    accepted = receipt("run", "attempt", "exit", a, order=3)
    session_store.accept_native_boundary(root, accepted)
    assert session_store.accept_native_boundary(
        root, receipt("run", "attempt", "exit", b, order=4)
    ) == session_store.BoundaryAcceptance(None, True)
    journal = root / "sessions.jsonl"
    durable = journal.read_bytes()
    assert b'"action":"invalidate_exit"' in durable
    assert session_store.accept_native_boundary(root, accepted).invalidated
    assert session_store.get_native_attempt_boundaries(root, "run", "attempt").exit_chat_id is None
    assert session_store.get_native_session_key(root, "c1") == a
    assert journal.read_bytes() == durable


def test_torn_tail_after_live_exit_blocks_unrelated_writer_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    native_key = key("/native/live-exit")
    session_store.accept_native_boundary(root, receipt("run", "attempt", "exit", native_key))
    journal = root / "sessions.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"event":"native_attempt"')
    preserved = journal.read_bytes()
    with pytest.raises(ValueError, match="Torn sessions tail"):
        session_store.record_model_observation(
            root,
            session_store.SessionModelObservationEvent(
                harness="pi",
                harness_session_id="native-other",
                observed_model_token="model",
                recorded_at="2026-01-01T00:00:00Z",
            ),
        )
    assert journal.read_bytes() == preserved


def test_complete_final_object_without_newline_is_replayed_before_append(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    key_value = key("/native/complete")
    event = session_store.accept_native_boundary(
        root, receipt("run", "attempt", "entry", key_value)
    )
    journal = root / "sessions.jsonl"
    journal.write_bytes(journal.read_bytes().rstrip(b"\n"))
    assert (
        session_store.accept_native_boundary(
            root, receipt("run", "attempt", "entry", key_value)
        ).chat_id
        == event.chat_id
    )
    assert journal.read_bytes().endswith(b"\n")


def test_torn_tail_without_any_exit_is_repaired_only_to_valid_prefix(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    journal = root / "sessions.jsonl"
    prefix = journal.read_bytes()
    journal.write_bytes(prefix + b'{"event":"native_attempt"')
    begin(root, "next", "next-attempt")
    assert journal.read_bytes().startswith(prefix)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [row["attempt_id"] for row in rows] == ["attempt", "next-attempt"]


def test_corrupt_interior_and_schema_invalid_rows_preserve_evidence(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    journal = root / "sessions.jsonl"
    journal.write_bytes(b'{"event":"other"}\nnot-json\n')
    original = journal.read_bytes()
    with pytest.raises(ValueError, match="Unsupported"):
        session_store.begin_native_attempt(
            root, "run", "attempt", transport_scope_id="transport:attempt"
        )
    assert journal.read_bytes() == original

    journal.write_bytes(b'{"event":"native_attempt","action":"begin"}\n')
    original = journal.read_bytes()
    with pytest.raises(ValueError, match="Invalid"):
        session_store.begin_native_attempt(
            root, "run", "attempt", transport_scope_id="transport:attempt"
        )
    assert journal.read_bytes() == original


def test_unqualified_raw_receipt_and_relative_store_are_rejected() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        key("relative/from-cwd")
    with pytest.raises(ValidationError):
        session_store.OwnedBoundaryReceipt.model_validate(
            {"run_id": "r", "attempt_id": "a", "boundary": "entry", "key": {}, "evidence": "native"}
        )


def test_append_fsync_ambiguity_is_replayed_and_confirmed(tmp_path: Path, monkeypatch) -> None:
    from meridian.lib.state import event_store

    root = tmp_path / "runtime"
    root.mkdir()
    original = event_store.append_durable_jsonl_line

    def append_then_report_failure(path: Path, line: str) -> None:
        original(path, line)
        raise OSError("simulated lost fsync acknowledgement")

    monkeypatch.setattr(event_store, "append_durable_jsonl_line", append_then_report_failure)
    begin(root, "run", "attempt")
    rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [row["action"] for row in rows] == ["begin"]
