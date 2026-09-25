"""Native search projection is disposable, scoped, and never binding authority."""

from __future__ import annotations

import json
import sqlite3

from meridian.lib.ops.session_index import SessionIndexInput, session_index_sync
from meridian.lib.ops.session_search import (
    SessionSearchInput,
    iter_session_subset_search,
    session_search_sync,
)
from meridian.lib.state import session_store
from meridian.lib.state.native_search_index import NativeSearchIndex
from tests.support.opencode_db import write_opencode_db_session


def corpus(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid="native-search"\n')
    from meridian.lib.state.user_paths import get_project_home

    root = get_project_home("native-search")
    root.mkdir(parents=True)
    store = tmp_path / "native"
    store.mkdir()
    return project, root, store


def write_native(path, text):
    path.write_text(
        json.dumps({"sessionId": path.stem})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": text}})
        + "\n"
    )


def test_refresh_rebuild_unbind_and_browse(tmp_path, monkeypatch):
    project, root, store = corpus(tmp_path, monkeypatch)
    chat = session_store.start_session(
        root, harness="claude", harness_session_id="one", native_store=str(store), model="test"
    )
    path = store / "one.jsonl"

    def search(query):
        return session_search_sync(SessionSearchInput(query=query, project_root=str(project)))

    write_native(path, "first needle")
    assert search("first").matches[0].open_command.startswith(f"meridian session log {chat} ")
    write_native(path, "second needle")
    assert search("first").matches == ()
    second = search("second")
    assert second.complete and len(second.matches) == 1
    index_path = NativeSearchIndex.for_runtime(root).path
    indexed_bytes = index_path.read_bytes()
    session_index_sync(
        SessionIndexInput(project_root=str(project), action="rebuild", metadata_only=True)
    )
    assert index_path.read_bytes() == indexed_bytes
    write_native(path, "third needle")
    third = search("third")
    assert third.complete
    assert next(
        iter_session_subset_search(project_root=str(project), chat_ids=[chat], query="third")
    ).matched
    queries = ("first", "second", "third")
    before = {query: search(query).matches for query in queries}
    index_path.unlink()
    session_index_sync(SessionIndexInput(project_root=str(project), action="rebuild"))
    assert {query: search(query).matches for query in queries} == before
    # Authoritative deletion must remove cached rows; cache cannot keep ownership alive.
    (root / "sessions.jsonl").write_text("")
    assert search("third").matches == ()
    assert NativeSearchIndex.for_runtime(root).inventory() == {}


def test_cold_budget_reports_coverage_without_partial_source_hits(tmp_path, monkeypatch):
    from meridian.lib.ops import session_search

    project, root, store = corpus(tmp_path, monkeypatch)
    session_store.start_session(
        root, harness="claude", harness_session_id="one", native_store=str(store), model="test"
    )
    write_native(store / "one.jsonl", "needle")
    monkeypatch.setattr(session_search, "INITIALIZATION_TIMEOUT", 0)
    output = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
    assert not output.complete
    assert output.sources_not_searched == 1
    assert "1 of 1 sources not searched" in output.format_text()
    assert not output.truncated


def test_opencode_in_place_update_and_shared_chat_owners(tmp_path, monkeypatch):
    project, root, store = corpus(tmp_path, monkeypatch)
    db = store / "opencode.db"
    write_opencode_db_session(
        db_path=db, session_id="ses_one", messages=[("assistant", "original needle")]
    )
    chats = tuple(
        session_store.start_session(
            root,
            harness="opencode",
            harness_session_id="ses_one",
            native_store=str(db),
            model="test",
        )
        for _ in range(2)
    )

    def search(query):
        return session_search_sync(SessionSearchInput(query=query, project_root=str(project)))

    before = search("original")
    assert len(before.matches) == 1
    assert set(before.matches[0].chat_ids) == set(chats)
    with sqlite3.connect(db) as writer:
        writer.execute(
            "UPDATE part SET time_updated=time_updated+1,data=?",
            (json.dumps({"type": "text", "text": "replacement needle"}),),
        )
    assert search("original").matches == ()
    assert len(search("replacement").matches) == 1


def test_bindings_follow_accepted_key_across_keyless_and_conflicting_starts(tmp_path):
    from meridian.lib.core.native_identity import NativeKey
    from meridian.lib.ops.session_search_index import native_bindings

    root = tmp_path / "rt"
    root.mkdir()
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id="one",
        native_store="/native",
        model="test",
        chat_id="c1",
    )
    session_store.stop_session(root, "c1")
    session_store.start_session(
        root, harness="claude", harness_session_id=None, model="test", chat_id="c1"
    )
    assert native_bindings(root) == {NativeKey("claude", "/native", "one"): ("c1",)}
    # Direct legacy event replay exercises the authority's conflict rule.
    events = (root / "sessions.jsonl").read_text().splitlines()
    conflicting = json.loads(events[-1])
    conflicting["harness_session_id"] = "other"
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write(json.dumps(conflicting) + "\n")
    assert native_bindings(root) == {NativeKey("claude", "/native", "one"): ("c1",)}


def test_corrupt_projection_rebuilds_and_old_sqlite_scans(tmp_path, monkeypatch):
    from meridian.lib.state import native_search_index

    project, root, store = corpus(tmp_path, monkeypatch)
    session_store.start_session(
        root, harness="claude", harness_session_id="one", native_store=str(store), model="test"
    )
    write_native(store / "one.jsonl", "needle")
    index = NativeSearchIndex.for_runtime(root)
    index.path.write_bytes(b"not sqlite")
    payload = SessionSearchInput(query="needle", project_root=str(project))
    assert session_search_sync(payload).complete
    assert len(session_search_sync(payload).matches) == 1
    monkeypatch.setattr(native_search_index.sqlite3, "sqlite_version_info", (3, 42, 0))
    fallback = session_search_sync(payload)
    assert fallback.complete and len(fallback.matches) == 1


def test_work_filter_keeps_only_in_scope_owners_without_pruning_other_keys(tmp_path, monkeypatch):
    project, root, store = corpus(tmp_path, monkeypatch)
    chats = []
    for sid in ("one", "two"):
        chats.append(
            session_store.start_session(
                root,
                harness="claude",
                harness_session_id=sid,
                native_store=str(store),
                model="test",
            )
        )
        write_native(store / f"{sid}.jsonl", "needle")
    session_store.update_session_work_id(root, chats[0], "work")
    all_hits = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
    assert len(all_hits.matches) == 2
    scoped = session_search_sync(
        SessionSearchInput(query="needle", work_id=" work ", project_root=str(project))
    )
    assert [hit.chat_id for hit in scoped.matches] == [chats[0]]
    assert len(NativeSearchIndex.for_runtime(root).inventory()) == 2


def test_corpus_preserves_ref_search_readiness_when_renderer_warns(tmp_path, monkeypatch):
    from tests.support.opencode_db import write_opencode_db_session_with_parts

    project, root, store = corpus(tmp_path, monkeypatch)
    db = store / "opencode.db"
    write_opencode_db_session_with_parts(
        db_path=db,
        session_id="ses_one",
        messages=[
            (
                "assistant",
                {},
                [
                    {"type": "text", "text": "confirmed needle"},
                    {"type": "tool", "tool": "future", "state": {"status": "future"}},
                ],
            )
        ],
    )
    chat = session_store.start_session(
        root, harness="opencode", harness_session_id="ses_one", native_store=str(db), model="test"
    )
    direct = session_search_sync(
        SessionSearchInput(query="needle", ref=chat, project_root=str(project))
    )
    projected = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
    assert len(direct.matches) == 1 and direct.complete
    assert len(projected.matches) == len(direct.matches)
    assert projected.matches[0].entry_ordinal == direct.matches[0].entry_ordinal
    assert projected.complete
    assert projected.warnings and not projected.errors
    assert projected.model_dump()["warnings"]
    assert "1 sources searched with warnings (see --json)" in projected.format_text()
    assert "future" not in projected.format_text().split("Open:")[-1]
    assert projected.sources_not_searched == 0
