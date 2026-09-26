"""Conversation model selection is durable history, independent of session liveness."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.state import session_store as store


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
            NativeKeyFields(session_id="thread"),
            session_instance_id=generation,
            startup_attempt_id="failed-attempt",
            source="observed",
        )
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "accepted-attempt")
        assert current(tmp_path, "thread") is None
        assert selected(tmp_path, "c1", generation, "p2", "astra", "thread", "next-invocation")
        store.update_session_harness_id(
            tmp_path,
            "c1",
            NativeKeyFields(session_id="thread"),
            session_instance_id=generation,
            startup_attempt_id="accepted-attempt",
            source="observed",
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

            patch.setattr(store, "append_event", fail)
            with pytest.raises(OSError, match="selection append failed"):
                selected(tmp_path, "c1", generation, "p3", "new")
        assert current(tmp_path) == "astra"
    finally:
        store.stop_session(tmp_path, "c1")


def test_old_generation_id_cannot_bind_new_pending_selection(tmp_path: Path) -> None:
    old = start(tmp_path, "c1", "")
    store.update_session_harness_id(
        tmp_path,
        "c1",
        NativeKeyFields(session_id="old-thread"),
        session_instance_id=old,
        startup_attempt_id="attempt-1",
        source="observed",
    )
    store.stop_session(tmp_path, "c1")
    new = start(tmp_path, "c1", "")
    try:
        assert selected(tmp_path, "c1", new, "p2", "astra", None)
        assert current(tmp_path, "old-thread") is None
        store.update_session_harness_id(
            tmp_path,
            "c1",
            NativeKeyFields(session_id="old-thread"),
            session_instance_id=new,
            startup_attempt_id="attempt-1",
            source="observed",
        )
        assert current(tmp_path, "new-thread") is None
        assert current(tmp_path, "old-thread") == "astra"
    finally:
        store.stop_session(tmp_path, "c1")


def test_unbound_duplicate_resolving_late_cannot_refresh_older_invocation(tmp_path: Path) -> None:
    generation = start(tmp_path, "c1", "")
    try:
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "first")
        assert selected(tmp_path, "c1", generation, "p2", "astra", "thread", "new")
        assert selected(tmp_path, "c1", generation, "p1", "sol", None, "retry")
        store.update_session_harness_id(
            tmp_path,
            "c1",
            NativeKeyFields(session_id="thread"),
            session_instance_id=generation,
            startup_attempt_id="retry",
            source="observed",
        )
        store.update_session_harness_id(
            tmp_path,
            "c1",
            NativeKeyFields(session_id="thread"),
            session_instance_id=generation,
            startup_attempt_id="first",
            source="observed",
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


def test_duplicate_invocation_attempt_link_replays_as_same_native_key(tmp_path: Path) -> None:
    import json

    from meridian.lib.state.native_binding import Same, bind

    generation = start(tmp_path, "c1")
    try:
        assert selected(tmp_path, "c1", generation, "p1", "sol")
        before = store.get_session_record(tmp_path, "c1")
        assert before is not None
        assert not selected(tmp_path, "c1", generation, "p1", "sol", attempt="retry")
        last = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[-1])
        link = store.SessionUpdateEvent.model_validate(last)
        assert link.startup_attempt_id == "retry"
        assert bind(before.key_fields(), link.key_fields()) == Same(before.key_fields())
        assert store.get_session_record(tmp_path, "c1") == before
        assert store.list_session_generations(tmp_path) == (before,)
    finally:
        store.stop_session(tmp_path, "c1")
