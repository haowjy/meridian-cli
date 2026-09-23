from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.lib.platform.locking import try_lock_file
from meridian.lib.state import session_store


def _contradiction_contender(
    root: str, attempted: object, acquired: object, acknowledged: object, result: object
) -> None:
    paths = session_store.RuntimePaths.from_root_dir(Path(root))
    with try_lock_file(paths.sessions_flock, reentrant=False) as handle:
        attempted.set()  # type: ignore[attr-defined]  # reached the independent lock attempt
        acquired.set()  # type: ignore[attr-defined]  # lock outcome is now observable
        result.put(handle is not None)  # type: ignore[attr-defined]
    session_store.accept_native_boundary(
        Path(root), receipt("run", "attempt", "exit", key("/native/repair-race-b"), order=2)
    )
    acknowledged.set()  # type: ignore[attr-defined]  # outcome/action is now observable


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


def test_allocation_skips_historical_nested_chat_reference(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    historical = session_store.SessionRecord(
        chat_id="c1",
        history_id=uuid.uuid4(),
        record_mode="historical",
        kind="primary",
        harness="pi",
        harness_session_id=None,
        harness_session_ids=(),
        model="",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(),
        params=(),
        started_at="2025-01-01T00:00:00Z",
        stopped_at="2025-01-01T00:01:00Z",
        session_instance_id="historical-generation",
    )
    # Older valid rows can predate the counter; occupancy must come from the
    # recognized nested historical schema, not only top-level chat_id fields.
    session_store._append_session_row(
        session_store.RuntimePaths.from_root_dir(root).sessions_jsonl,
        session_store.SessionHistoricalEvent(record=historical),
    )
    begin(root, "new-run", "new-attempt")
    allocated = session_store.accept_native_boundary(
        root, receipt("new-run", "new-attempt", "entry", key("/native/new"))
    ).chat_id
    assert allocated == "c2"


def test_restore_import_requires_canonical_plan_and_published_aggregate(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "operational-run", "operational-attempt")
    occupied = session_store.accept_native_boundary(
        root, receipt("operational-run", "operational-attempt", "entry", key("/native/live"))
    ).chat_id
    assert occupied == "c1"

    historical = session_store.SessionRecord(
        chat_id=occupied,
        history_id=uuid.uuid4(),
        record_mode="historical",
        kind="primary",
        harness="pi",
        harness_session_id=None,
        harness_session_ids=(),
        model="",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(),
        params=(),
        started_at="2025-01-01T00:00:00Z",
        stopped_at="2025-01-01T00:01:00Z",
        session_instance_id="restored-generation",
        spawn_id="restored-spawn",
    )
    plan_path = root / "history-archives" / "restores" / f"{historical.history_id}.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(
            {
                "portable_digest": "0" * 64,
                "local_id": historical.spawn_id,
                "chat_id": historical.chat_id,
                "generation": historical.session_instance_id,
                "session": historical.model_dump(mode="json"),
            }
        )
    )
    with pytest.raises(ValueError, match="published inert aggregate"):
        session_store.append_historical_session(root, historical, history_id=historical.history_id)

    unrelated = historical.model_copy(update={"chat_id": "c2"})
    with pytest.raises(ValueError, match="does not own"):
        session_store.append_historical_session(root, unrelated, history_id=historical.history_id)
    plan_path.unlink()
    with pytest.raises(ValueError, match="no valid durable restore plan"):
        session_store.append_historical_session(root, historical, history_id=historical.history_id)


@pytest.mark.parametrize("store", [
    "relative/db", "/x/./y", "/x//y", "/x/y/", "/x/\x00y",
    "namespace://db/x", "namespace:v1://server/db?", "namespace:v1://server/db#",
    "namespace:v1://ser\tver/db", "namespace:v1://server/line\nbreak",
])
def test_native_store_identity_rejects_noncanonical_lexical_forms(store: str) -> None:
    with pytest.raises(ValidationError):
        key(store)


def test_versioned_native_store_namespace_is_explicit_and_canonical() -> None:
    first = key("namespace:v1://server/database", "native")
    second = key("namespace:v1://other/database", "native")
    assert first.store == "namespace:v1://server/database"
    assert first != second
    with pytest.raises(ValidationError):
        key("namespace:v1://server/db/../database", "native")


@pytest.mark.parametrize("chat_refs, expected", [
    ([" c1 "], "c2"),
    (["c¹"], "c1"),
    (["c10"], "c11"),
])
def test_reservation_uses_normalized_ascii_high_water(
    chat_refs: list[str], expected: str, tmp_path: Path
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    sessions = root / "sessions.jsonl"
    sessions.write_text(
        "".join(json.dumps({"event": "stop", "chat_id": ref}) + "\n" for ref in chat_refs)
    )
    assert session_store.reserve_chat_id(root) == expected


def test_start_cannot_turn_historical_reference_live(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    historical = session_store.SessionRecord(
        chat_id="c1", record_mode="historical", kind="primary", harness="pi",
        harness_session_id=None,
        harness_session_ids=(),
        model="",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(), params=(), started_at="2025-01-01T00:00:00Z",
        stopped_at="2025-01-01T00:01:00Z", session_instance_id="historical-generation",
    )
    sessions = root / "sessions.jsonl"
    historical_event = session_store.SessionHistoricalEvent(record=historical)
    sessions.write_text(json.dumps(historical_event.model_dump(mode="json")) + "\n")
    before = sessions.read_bytes()
    with pytest.raises(ValueError, match="cannot be started"):
        session_store.start_session(root, "pi", "native", "test", chat_id="c1")
    assert sessions.read_bytes() == before
    assert not (root / "sessions" / "c1.lease.json").exists()


def test_reservation_fails_closed_on_corrupt_counter(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "session-id-counter").write_text("not-a-counter\n")
    with pytest.raises(ValueError, match="counter is corrupt"):
        session_store.reserve_chat_id(root)


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


def test_repair_holds_session_lock_until_replacement_and_contender_acknowledges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    first = key("/native/repair-race-a")
    session_store.accept_native_boundary(root, receipt("run", "attempt", "exit", first))
    journal = root / "sessions.jsonl"
    journal.write_bytes(journal.read_bytes().rstrip(b"\n"))

    context = multiprocessing.get_context("fork")
    repair_reached = context.Event()
    allow_repair_to_finish = context.Event()
    repair_hook = (
        "atomic_write_bytes"
        if hasattr(session_store, "atomic_write_bytes")
        else "atomic_write_text"
    )
    original_repair = getattr(session_store, repair_hook)

    parent_pid = os.getpid()

    def pause_before_repair(
        path: Path, content: bytes | str, *args: object, **kwargs: object
    ) -> None:
        # fork inherits monkeypatches; only the stale parent replacement is paused.
        if path == journal and os.getpid() == parent_pid:
            repair_reached.set()
            assert allow_repair_to_finish.wait(5)
        original_repair(path, content, *args, **kwargs)

    monkeypatch.setattr(session_store, repair_hook, pause_before_repair)
    with ThreadPoolExecutor(max_workers=1) as pool:
        start = pool.submit(session_store.start_session, root, "pi", "spawn-session", "model")
        assert repair_reached.wait(5)
        attempted, acquired, acknowledged = context.Event(), context.Event(), context.Event()
        lock_result = context.Queue()
        contender = context.Process(
            target=_contradiction_contender,
            args=(str(root), attempted, acquired, acknowledged, lock_result),
        )
        contender.start()
        assert attempted.wait(5)
        assert acquired.wait(5)
        lock_was_available = lock_result.get(timeout=5)
        if lock_was_available:
            # An unlocked preflight permits destructive replacement: make the
            # contender's invalidation durable before releasing the stale bytes.
            assert acknowledged.wait(5)
        allow_repair_to_finish.set()
        chat_id = start.result(timeout=5)
        assert acknowledged.wait(5)
        contender.join(5)
        assert contender.exitcode == 0
    try:
        assert session_store.get_native_attempt_boundaries(root, "run", "attempt").exit_invalidated
        assert not lock_was_available, "contender acquired sessions lock during stale replacement"
    finally:
        session_store.stop_session(root, chat_id)


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
    root = tmp_path / "runtime"
    root.mkdir()
    original = session_store._append_session_row

    def append_then_report_failure(path: Path, event: object, **kwargs: object) -> None:
        original(path, event, **kwargs)  # type: ignore[arg-type]
        raise OSError("simulated lost fsync acknowledgement")

    monkeypatch.setattr(session_store, "_append_session_row", append_then_report_failure)
    begin(root, "run", "attempt")
    rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [row["action"] for row in rows] == ["begin"]


def test_duplicate_begin_cannot_acknowledge_persistently_failed_file_sync(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    original_fsync = session_store.os.fsync

    def fail_sync(_fd: int) -> None:
        raise OSError("persistent file sync failure")

    monkeypatch.setattr(session_store.os, "fsync", fail_sync)
    for _ in range(2):
        with pytest.raises(OSError, match="persistent file sync failure"):
            begin(root, "run", "attempt")
    monkeypatch.setattr(session_store.os, "fsync", original_fsync)
    begin(root, "run", "attempt")
    rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [row["action"] for row in rows] == ["begin"]


def test_duplicate_begin_cannot_acknowledge_failed_publication_directory_sync(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("persistent directory sync failure")

    monkeypatch.setattr(session_store, "fsync_directory", fail_directory_sync)
    for _ in range(2):
        with pytest.raises(OSError, match="persistent directory sync failure"):
            begin(root, "run", "attempt")
    monkeypatch.undo()
    begin(root, "run", "attempt")
    rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [row["action"] for row in rows] == ["begin"]


@pytest.mark.parametrize("getter", ["key", "boundaries"])
def test_confirming_getters_reject_persistent_file_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, getter: str
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    native_key = key("/native/confirmed-read")
    session_store.accept_native_boundary(root, receipt("run", "attempt", "exit", native_key))

    def fail_sync(_fd: int) -> None:
        raise OSError("persistent file sync failure")

    monkeypatch.setattr(session_store.os, "fsync", fail_sync)
    for _ in range(2):
        with pytest.raises(OSError, match="persistent file sync failure"):
            if getter == "key":
                session_store.get_native_session_key(root, "c1")
            else:
                session_store.get_native_attempt_boundaries(root, "run", "attempt")


def test_failed_refutation_cannot_be_hidden_by_confirming_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    first = key("/native/refutation-first")
    second = key("/native/refutation-second")
    session_store.accept_native_boundary(root, receipt("run", "attempt", "exit", first))

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("persistent directory sync failure")

    monkeypatch.setattr(session_store, "fsync_directory", fail_directory_sync)
    with pytest.raises(OSError, match="persistent directory sync failure"):
        session_store.accept_native_boundary(
            root, receipt("run", "attempt", "exit", second, order=2)
        )
    with pytest.raises(OSError, match="persistent directory sync failure"):
        session_store.get_native_attempt_boundaries(root, "run", "attempt")


@pytest.mark.parametrize("fault", ["file", "directory"])
def test_first_begin_write_failure_retry_commits_one_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    original_fsync = session_store.os.fsync
    original_directory_sync = session_store.fsync_directory

    if fault == "file":
        def fail_sync(_fd: int) -> None:
            raise OSError("initial file sync failure")

        monkeypatch.setattr(session_store.os, "fsync", fail_sync)
    else:
        def fail_directory_sync(_path: Path) -> None:
            raise OSError("initial directory sync failure")

        monkeypatch.setattr(session_store, "fsync_directory", fail_directory_sync)
    with pytest.raises(OSError, match=r"initial .* sync failure"):
        begin(root, "first", "attempt")
    with pytest.raises(OSError, match=r"initial .* sync failure"):
        begin(root, "first", "attempt")
    monkeypatch.setattr(session_store.os, "fsync", original_fsync)
    monkeypatch.setattr(session_store, "fsync_directory", original_directory_sync)
    begin(root, "first", "attempt")
    rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [(row["run_id"], row["action"]) for row in rows] == [("first", "begin")]


def test_model_writers_do_not_recreate_a_missing_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "deleted-runtime"
    selection = session_store.SessionModelSelectionEvent(
        kind="initial_seed",
        harness="pi",
        harness_session_id="native",
        spawn_id=None,
        startup_attempt_id=None,
        selection=session_store.ConversationModelSelection(
            requested_token="model",
            selected_token="model",
            canonical_model_id="model",
            harness_model_id="model",
            model_mode="named",
            selection_source="initial_launch",
        ),
        recorded_at="2026-01-01T00:00:00Z",
        chat_id="c1",
        session_instance_id="generation",
    )
    observation = session_store.SessionModelObservationEvent(
        harness="pi",
        harness_session_id="native",
        observed_model_token="model",
        recorded_at="2026-01-01T00:00:00Z",
    )
    for writer, event in (
        (session_store.record_model_selection, selection),
        (session_store.record_model_observation, observation),
    ):
        with pytest.raises(FileNotFoundError):
            writer(root, event)  # type: ignore[arg-type]
        assert not root.exists()


@pytest.mark.parametrize("counter", [None, "0\n"])
def test_native_only_reservation_recovers_journal_high_water(
    tmp_path: Path, counter: str | None
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    begin(root, "run", "attempt")
    assert session_store.accept_native_boundary(
        root, receipt("run", "attempt", "entry", key("/native/high-water"))
    ).chat_id == "c1"
    paths = session_store.RuntimePaths.from_root_dir(root)
    if counter is None:
        paths.session_id_counter.unlink()
    else:
        paths.session_id_counter.write_text(counter)
    assert session_store.reserve_chat_id(root) == "c2"
