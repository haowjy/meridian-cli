from __future__ import annotations

from pathlib import Path

from meridian.lib.state import session_identity, session_store, spawn_store


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def test_spawn_owner_and_exact_chat_ids(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c-child",
            owner_chat_id="c-owner",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="child",
        )
    )
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert session_identity.spawn_exact_chat_id(row) == "c-child"
    assert session_identity.spawn_owner_chat_id(row) == "c-owner"
    assert session_identity.spawn_matches_owner_chat(row, "c-owner")
    assert not session_identity.spawn_matches_exact_session(row, "c-owner")
    assert session_identity.spawn_matches_exact_session(row, "c-child")


def test_get_session_record_for_spawn_links_via_spawn_id(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c1",
            owner_chat_id="c-owner",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="child",
        )
    )
    child_chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-child",
        model="gpt-5.4",
        chat_id="c-child",
        spawn_id=spawn_id,
    )
    try:
        record = session_identity.get_session_record_for_spawn(runtime_root, spawn_id)
        assert record is not None
        assert record.chat_id == child_chat_id
        assert record.harness_session_id == "thread-child"
    finally:
        session_store.stop_session(runtime_root, child_chat_id)


def test_is_tracked_chat_ref_accepts_named_chat_ids(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    owner_chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="ses-owner",
        model="gpt-5.4",
        chat_id="c-owner",
        kind="primary",
    )
    try:
        assert session_identity.is_tracked_chat_ref(runtime_root, "c-owner")
        assert session_identity.is_tracked_chat_ref(runtime_root, owner_chat_id)
        assert not session_identity.is_tracked_chat_ref(runtime_root, "c-missing")
    finally:
        session_store.stop_session(runtime_root, owner_chat_id)


def test_get_session_record_for_spawn_rejects_parent_chat_for_child(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    parent_chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="parent-session-id",
        model="gpt-5.4",
        chat_id="c-parent",
        kind="primary",
    )
    spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            spawn_id="p42",
            chat_id=parent_chat_id,
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="child",
            harness_session_id="",
        )
    )
    try:
        assert (
            session_identity.get_session_record_for_spawn(
                runtime_root,
                spawn_id,
                require_harness_session_id=True,
            )
            is None
        )
    finally:
        session_store.stop_session(runtime_root, parent_chat_id)


def test_session_owner_chat_id_for_child_session(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    owner_chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="ses-owner",
        model="gpt-5.4",
        chat_id="c-owner",
        kind="primary",
    )
    spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c-child",
            owner_chat_id=owner_chat_id,
            model="gpt-5.4",
            agent="coder",
            harness="pi",
            kind="child",
            prompt="child",
        )
    )
    child_chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="",
        model="gpt-5.4",
        chat_id="c-child",
        spawn_id=spawn_id,
    )
    try:
        child_record = session_store.get_session_record(runtime_root, child_chat_id)
        assert child_record is not None
        assert session_identity.session_owner_chat_id(runtime_root, child_record) == owner_chat_id
        assert (
            session_identity.get_owner_chat_for_session(runtime_root, child_chat_id)
            == owner_chat_id
        )
    finally:
        session_store.stop_session(runtime_root, child_chat_id)
        session_store.stop_session(runtime_root, owner_chat_id)
