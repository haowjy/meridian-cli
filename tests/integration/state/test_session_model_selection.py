"""Conversation model selection is durable history, independent of session liveness."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest


def _hard_deny(*args: object, **kwargs: object) -> None:
    _ = args, kwargs
    raise AssertionError("process or network effect attempted")


_original_socket = socket.socket


class _InternetDeniedSocket(_original_socket):
    def __new__(cls, family=socket.AF_INET, *args, **kwargs):
        if family != socket.AF_UNIX:
            _hard_deny(family, *args, **kwargs)
        return super().__new__(cls, family, *args, **kwargs)


# Import the state surface with process and internet socket creation disabled.
_original_boundaries = (subprocess.Popen, os.system, socket.socket, socket.create_connection)
subprocess.Popen = _hard_deny  # type: ignore[assignment]
os.system = _hard_deny  # type: ignore[assignment]
socket.socket = _InternetDeniedSocket  # type: ignore[assignment]
socket.create_connection = _hard_deny  # type: ignore[assignment]
try:
    from meridian.lib.state import session_store as store
finally:
    subprocess.Popen, os.system, socket.socket, socket.create_connection = _original_boundaries


def _deny_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def denied(label: str):
        def fail(*args: object, **kwargs: object) -> None:
            _ = args, kwargs
            attempts.append(label)
            raise AssertionError(f"unexpected {label} effect")

        return fail

    monkeypatch.setattr(subprocess, "Popen", denied("process"))
    monkeypatch.setattr(os, "system", denied("process"))
    class DeniedInternetSocket(_original_socket):
        def __new__(cls, family=socket.AF_INET, *args, **kwargs):
            if family != socket.AF_UNIX:
                return denied("network")(family, *args, **kwargs)
            return super().__new__(cls, family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", DeniedInternetSocket)
    monkeypatch.setattr(socket, "create_connection", denied("network"))
    return attempts


@pytest.fixture(autouse=True)
def _guard_exact_model_tests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    guarded_prefixes = (
        "test_exact_",
        "test_same_native_id_",
        "test_two_stores_",
        "test_conflicting_startup_",
        "test_deferred_",
    )
    if request.node.name.startswith(guarded_prefixes):
        _deny_effects(monkeypatch)


def start(root: Path, chat: str, native: str = "thread") -> str:
    store.start_session(root, "codex", native, "sol", chat_id=chat)
    record = store.get_session_record(root, chat)
    assert record is not None
    return record.session_instance_id


def selected(
    root: Path,
    chat: str,
    generation: str,
    spawn: str,
    model: str,
    native: str | None = "thread",
    attempt: str = "attempt-1",
    kind: str = "invocation_started",
) -> bool:
    return store.record_model_selection(
        root,
        store.SessionModelSelectionEvent(
            kind=kind,
            chat_id=chat,
            session_instance_id=generation,
            spawn_id=spawn,
            harness="codex",
            harness_session_id=native,
            startup_attempt_id=attempt,
            selection=store.ConversationModelSelection(
                requested_token=model,
                selected_token=model,
                canonical_model_id=model,
                harness_model_id=model,
                model_mode="named",
                selection_source="explicit_override",
            ),
            recorded_at="2026-09-15T00:00:00Z",
        ),
    )


def current(root: Path, native: str = "thread") -> str | None:
    value = store.get_model_selection(root, "codex", native)
    return value.canonical_model_id if value else None


def test_selection_order_and_dedup_do_not_rewrite_launch_history(tmp_path: Path) -> None:
    generation = start(tmp_path, "c1")
    before = store.get_session_record(tmp_path, "c1")
    try:
        assert selected(tmp_path, "c1", generation, "p1", "sol")
        assert selected(tmp_path, "c1", generation, "p2", "astra")
        assert not selected(tmp_path, "c1", generation, "p1", "sol", attempt="retry")
        assert current(tmp_path) == "astra"
        assert selected(
            tmp_path, "c1", generation, "p1", "sol", native="other", attempt="fresh-retry"
        )
        assert current(tmp_path, "other") == "sol"
        assert store.get_model_selection(tmp_path, "claude", "thread") is None
        assert store.get_session_record(tmp_path, "c1") == before
        assert store.get_last_session(tmp_path) == before
    finally:
        store.stop_session(tmp_path, "c1")


def test_pending_selection_binds_only_its_attempt_without_reordering(tmp_path: Path) -> None:
    generation = start(tmp_path, "c1", "")
    try:
        store.update_session_harness_id(
            tmp_path,
            "c1",
            "wrong-attempt",
            session_instance_id=generation,
            startup_attempt_id="failed-attempt",
        )
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "accepted-attempt")
        assert current(tmp_path, "wrong-attempt") is None
        assert selected(tmp_path, "c1", generation, "p2", "astra", "thread", "next-invocation")
        store.update_session_harness_id(
            tmp_path,
            "c1",
            "thread",
            session_instance_id=generation,
            startup_attempt_id="accepted-attempt",
        )
        assert current(tmp_path) == "astra"
        assert not selected(tmp_path, "c1", generation, "p1", "sol", "thread", "retry")
        before = (tmp_path / "sessions.jsonl").read_bytes()
        assert current(tmp_path) == "astra"
        assert (tmp_path / "sessions.jsonl").read_bytes() == before
    finally:
        store.stop_session(tmp_path, "c1")


def test_seed_cannot_replace_started_selection_and_write_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = start(tmp_path, "c1")
    try:
        assert selected(tmp_path, "c1", generation, "p1", "sol", kind="initial_seed")
        assert selected(tmp_path, "c1", generation, "p2", "astra")
        assert not selected(tmp_path, "c1", generation, "p0", "sol", kind="initial_seed")
        with monkeypatch.context() as patch:

            def fail(*args: object, **kwargs: object) -> None:
                raise OSError("selection append failed")

            patch.setattr(store, "_append_session_row", fail)
            with pytest.raises(OSError, match="selection append failed"):
                selected(tmp_path, "c1", generation, "p3", "new")
        assert current(tmp_path) == "astra"
    finally:
        store.stop_session(tmp_path, "c1")


def test_old_generation_id_cannot_bind_new_pending_selection(tmp_path: Path) -> None:
    old = start(tmp_path, "c1", "")
    store.update_session_harness_id(
        tmp_path, "c1", "old-thread", session_instance_id=old, startup_attempt_id="attempt-1"
    )
    store.stop_session(tmp_path, "c1")
    new = start(tmp_path, "c1", "")
    try:
        assert selected(tmp_path, "c1", new, "p2", "astra", None)
        assert current(tmp_path, "old-thread") is None
        store.update_session_harness_id(
            tmp_path, "c1", "new-thread", session_instance_id=new, startup_attempt_id="attempt-1"
        )
        assert current(tmp_path, "new-thread") == "astra"
        assert current(tmp_path, "old-thread") is None
    finally:
        store.stop_session(tmp_path, "c1")


def test_unbound_duplicate_resolving_late_cannot_refresh_older_invocation(tmp_path: Path) -> None:
    generation = start(tmp_path, "c1", "")
    try:
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "first")
        assert selected(tmp_path, "c1", generation, "p2", "astra", "thread", "new")
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "retry")
        store.update_session_harness_id(
            tmp_path, "c1", "thread", session_instance_id=generation, startup_attempt_id="retry"
        )
        store.update_session_harness_id(
            tmp_path, "c1", "thread", session_instance_id=generation, startup_attempt_id="first"
        )
        assert current(tmp_path) == "astra"
    finally:
        store.stop_session(tmp_path, "c1")


def _concurrent_selection(root: str, generation: str) -> bool:
    return selected(Path(root), "c1", generation, "p1", "astra")


def test_concurrent_append_deduplicates_under_the_session_log_lock(tmp_path: Path) -> None:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    generation = start(tmp_path, "c1")
    try:
        with ProcessPoolExecutor(
            max_workers=3, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(pool.map(_concurrent_selection, [str(tmp_path)] * 6, [generation] * 6))
        assert results.count(True) == 1
        assert current(tmp_path) == "astra"
        rows = (tmp_path / "sessions.jsonl").read_text().splitlines()
        assert len(rows) == 2  # One historical start, one durable selection.
    finally:
        store.stop_session(tmp_path, "c1")


def _exact_source(root: Path):
    import asyncio

    from meridian.lib.ops.reference import AuthorizedNativeTarget, resolve_native_reference

    target = asyncio.run(resolve_native_reference(root, "c1", purpose="read"))
    assert isinstance(target, AuthorizedNativeTarget)
    return target.source


def _append_start(root: Path, *, generation: str, spawn: str = "p1") -> None:
    event = store.SessionStartEvent(
        chat_id="c1",
        kind="primary",
        harness="pi",
        harness_session_id="conversation",
        model="policy-model",
        session_instance_id=generation,
        started_at="2026-09-24T00:00:00Z",
        spawn_id=spawn,
    )
    with (root / "sessions.jsonl").open("ab") as handle:
        handle.write((event.model_dump_json(exclude_none=True) + "\n").encode())


def _exact_journal(
    root: Path, store_path: str = "/synthetic/native"
) -> tuple[object, str]:
    from tests.integration.ops.test_native_reference_authority import _pinned_journal

    _pinned_journal(root, store_path)
    source = _exact_source(root)
    generation = "generation-exact"
    _append_start(root, generation=generation)
    return source, generation


def _v2_event(source, generation: str, model: str, *, startup: str = "attempt-a"):
    from meridian.lib.state.session_authority import SourceModelSelectionEvent

    return SourceModelSelectionEvent(
        kind="invocation_started",
        harness="pi",
        harness_session_id="conversation",
        chat_id="c1",
        session_instance_id=generation,
        spawn_id="p1",
        startup_attempt_id=startup,
        recorded_at="2026-09-24T00:00:00Z",
        source=source,
        selection=store.ConversationModelSelection(
            requested_token=model,
            selected_token=model,
            canonical_model_id=model,
            harness_model_id=model,
            model_mode="named",
            selection_source="recorded_selection",
            provenance={"source": "fixture"},
        ),
    )


def test_exact_model_facts_are_source_scoped_immutable_and_zero_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    assert store.record_model_selection(tmp_path, _v2_event(source, generation, "seed"))
    assert not store.record_model_selection(
        tmp_path, _v2_event(source, generation, "seed", startup="retry")
    )

    reads: list[Path] = []
    original = session_store_module.read_journal

    def count_read(raw: bytes):
        reads.append(tmp_path / "sessions.jsonl")
        return original(raw)

    monkeypatch.setattr(session_store_module, "read_journal", count_read)
    first = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(first, session_store_module.NativeSourceUseSnapshot)
    assert len(reads) == 1
    facts = first.replay_model_facts(source)
    assert isinstance(facts, authority.ReplayModelFacts)
    assert facts.latest_invocation is not None
    assert facts.latest_invocation.event.selection.canonical_model_id == "seed"
    facts.latest_invocation.event.selection.provenance["source"] = "mutated"
    assert first.replay_model_facts(source).latest_invocation.event.selection.provenance == {
        "source": "fixture"
    }
    assert len(reads) == 1


def test_same_native_id_in_another_store_does_not_share_v2_intent(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    assert store.record_model_selection(tmp_path, _v2_event(source, generation, "store-a"))
    other = source.model_copy(
        update={"key": source.key.model_copy(update={"store": "/synthetic/other"})}
    )
    snapshot = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    result = snapshot.replay_model_facts(other)
    assert isinstance(result, authority.FactsUnavailable)
    assert result.reason == "source_not_eligible"


def test_v1_model_intent_remains_legacy_unscoped_not_promoted(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    # A legacy row with the same chat/native ID is historical and cannot satisfy A.
    legacy = store.SessionModelSelectionEvent(
        kind="invocation_started",
        harness="pi",
        harness_session_id="conversation",
        chat_id="c1",
        session_instance_id=generation,
        spawn_id="p1",
        startup_attempt_id="legacy-attempt",
        recorded_at="2026-09-24T00:00:00Z",
        selection=store.ConversationModelSelection(
            requested_token="legacy",
            selected_token="legacy",
            canonical_model_id="legacy",
            harness_model_id="legacy",
            model_mode="named",
            selection_source="recorded_selection",
        ),
    )
    assert store.record_model_selection(tmp_path, legacy)
    snapshot = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    assert isinstance(snapshot.replay_model_facts(source), authority.ReplayModelFacts)
    assert snapshot.journal.metadata.model_intents[0].correlation == "legacy_unscoped"
    assert snapshot.replay_model_facts(source).latest_invocation is None


def test_two_stores_same_native_id_keep_model_facts_separate(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    root_a, root_b = tmp_path / "store-a", tmp_path / "store-b"
    source_a, generation_a = _exact_journal(root_a, "/synthetic/a")
    source_b, generation_b = _exact_journal(root_b, "/synthetic/b")
    assert source_a.key.native_session_id == source_b.key.native_session_id
    assert source_a.key.store != source_b.key.store
    assert store.record_model_selection(root_a, _v2_event(source_a, generation_a, "model-a"))
    assert store.record_model_selection(root_b, _v2_event(source_b, generation_b, "model-b"))

    snapshot_a = session_store_module.read_native_source_use_snapshot(root_a)
    snapshot_b = session_store_module.read_native_source_use_snapshot(root_b)
    assert isinstance(snapshot_a, session_store_module.NativeSourceUseSnapshot)
    assert isinstance(snapshot_b, session_store_module.NativeSourceUseSnapshot)
    facts_a = snapshot_a.replay_model_facts(source_a)
    facts_b = snapshot_b.replay_model_facts(source_b)
    assert isinstance(facts_a, authority.ReplayModelFacts)
    assert isinstance(facts_b, authority.ReplayModelFacts)
    assert facts_a.latest_invocation.event.selection.canonical_model_id == "model-a"
    assert facts_b.latest_invocation.event.selection.canonical_model_id == "model-b"
    assert isinstance(snapshot_a.replay_model_facts(source_b), authority.FactsUnavailable)


def test_exact_first_seed_latest_invocation_and_v1_membership_independence(
    tmp_path: Path,
) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, first_generation = _exact_journal(tmp_path)
    # Legacy membership for the same (harness, ID) must not hide the first exact v2 row.
    legacy = store.SessionModelSelectionEvent(
        kind="invocation_started",
        harness="pi",
        harness_session_id="conversation",
        chat_id="c1",
        session_instance_id=first_generation,
        spawn_id="p1",
        startup_attempt_id="attempt-a",
        recorded_at="2026-09-24T00:00:00Z",
        selection=store.ConversationModelSelection(
            requested_token="old",
            selected_token="old",
            canonical_model_id="old",
            harness_model_id="old",
            model_mode="named",
            selection_source="recorded_selection",
        ),
    )
    assert store.record_model_selection(tmp_path, legacy)

    seed = _v2_event(source, first_generation, "seed")
    seed = seed.model_copy(update={"kind": "initial_seed", "startup_attempt_id": None})
    assert store.record_model_selection(tmp_path, seed)
    assert store.record_model_selection(tmp_path, _v2_event(source, first_generation, "first"))
    second_generation = "generation-second"
    _append_start(tmp_path, generation=second_generation, spawn="p2")
    assert store.record_model_selection(
        tmp_path, _v2_event(source, second_generation, "latest", startup="attempt-b").model_copy(
            update={"spawn_id": "p2"}
        )
    )

    snapshot = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    facts = snapshot.replay_model_facts(source)
    assert isinstance(facts, authority.ReplayModelFacts)
    assert facts.latest_invocation.event.selection.canonical_model_id == "latest"
    assert facts.first_committed_seed.event.selection.canonical_model_id == "seed"
    assert facts.original_seed_recovery == "origin_not_correlated"


def test_exact_facts_refuse_duplicate_rows_and_contradictory_captured_start(
    tmp_path: Path,
) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    event = _v2_event(source, generation, "model")
    assert store.record_model_selection(tmp_path, event)
    with (tmp_path / "sessions.jsonl").open("ab") as handle:
        handle.write((event.model_dump_json(exclude_none=True) + "\n").encode())
    duplicate = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(duplicate, session_store_module.NativeIdUnavailable)
    assert duplicate.reason == "authority_invalid"

    # The next independent fixture proves a later contradictory start poisons use.
    other_root = tmp_path / "contradictory-start"
    source, generation = _exact_journal(other_root)
    assert store.record_model_selection(other_root, _v2_event(source, generation, "model"))
    contradictory = store.SessionStartEvent(
        chat_id="c1",
        kind="primary",
        harness="pi",
        harness_session_id="conversation",
        model="different-policy",
        session_instance_id=generation,
        started_at="2026-09-24T00:00:00Z",
        spawn_id="p1",
    )
    with (other_root / "sessions.jsonl").open("ab") as handle:
        handle.write((contradictory.model_dump_json(exclude_none=True) + "\n").encode())
    snapshot = session_store_module.read_native_source_use_snapshot(other_root)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    assert snapshot.replay_model_facts(source) == authority.FactsUnavailable("source_conflict")


def test_deferred_legacy_value_binds_once_and_contradiction_retracts_it(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority

    source, generation = _exact_journal(tmp_path)
    _ = source
    pending = store.SessionModelSelectionEvent(
        kind="invocation_started",
        harness="pi",
        harness_session_id=None,
        chat_id="c1",
        session_instance_id=generation,
        spawn_id="p1",
        startup_attempt_id="deferred",
        recorded_at="2026-09-24T00:00:00Z",
        selection=store.ConversationModelSelection(
            requested_token="legacy",
            selected_token="legacy",
            canonical_model_id="legacy",
            harness_model_id="legacy",
            model_mode="named",
            selection_source="recorded_selection",
        ),
    )
    assert store.record_model_selection(tmp_path, pending)
    for native in ("conversation", "other"):
        update = store.SessionUpdateEvent(
            chat_id="c1",
            session_instance_id=generation,
            startup_attempt_id="deferred",
            harness_session_id=native,
        )
        with (tmp_path / "sessions.jsonl").open("ab") as handle:
            handle.write((update.model_dump_json(exclude_none=True) + "\n").encode())
    journal = authority.read_journal((tmp_path / "sessions.jsonl").read_bytes())
    assert not any(
        fact.event.startup_attempt_id == "deferred"
        for fact in journal.snapshot.metadata.model_intents
    )
    assert journal.snapshot.metadata.selections == frozenset()


def test_conflicting_startup_update_retracts_exact_fact_as_unavailable(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    event = _v2_event(source, generation, "model")
    assert store.record_model_selection(tmp_path, event)
    for native in ("conversation", "other"):
        update = store.SessionUpdateEvent(
            chat_id="c1",
            session_instance_id=generation,
            startup_attempt_id="attempt-a",
            harness_session_id=native,
        )
        with (tmp_path / "sessions.jsonl").open("ab") as handle:
            handle.write((update.model_dump_json(exclude_none=True) + "\n").encode())
    snapshot = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    assert snapshot.replay_model_facts(source) == authority.FactsUnavailable("source_conflict")
    assert ("pi", "conversation") not in snapshot.journal.metadata.selections
    assert ("pi", "conversation", "p1") not in snapshot.journal.metadata.invocations


def test_identical_start_replay_and_wrong_source_ref_rejects_v2_append(tmp_path: Path) -> None:
    from meridian.lib.state import session_store as session_store_module

    source, generation = _exact_journal(tmp_path)
    snapshot = session_store_module.read_native_source_use_snapshot(tmp_path)
    assert isinstance(snapshot, session_store_module.NativeSourceUseSnapshot)
    captured = snapshot.journal.metadata.starts[("c1", generation, "pi")]
    with (tmp_path / "sessions.jsonl").open("ab") as handle:
        handle.write((captured.model_dump_json(exclude_none=True) + "\n").encode())

    event = _v2_event(source, generation, "model")
    assert store.record_model_selection(tmp_path, event)
    wrong_ref = source.model_copy(
        update={
            "ref": source.ref.model_copy(
                update={"locator_event_id": "0" * 64}
            )
        }
    )
    before = (tmp_path / "sessions.jsonl").read_bytes()
    with pytest.raises(ValueError, match="current exact pin"):
        store.record_model_selection(tmp_path, _v2_event(wrong_ref, generation, "wrong"))
    assert (tmp_path / "sessions.jsonl").read_bytes() == before
