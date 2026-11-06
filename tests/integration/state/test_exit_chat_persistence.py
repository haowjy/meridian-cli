"""Observed identities inherit context; verified exits additionally require a transcript."""

from functools import partial
from pathlib import Path
import pytest
from meridian.lib.state import session_identity, session_store, spawn_store


def test_verified_exit_publishes_only_with_native_proof_on_creation_and_reuse(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    store = str(tmp_path / "native")
    entry = session_store.start_session(
        runtime,
        harness="claude",
        harness_session_id="entry",
        model="",
        native_store=store,
    ).chat_id
    session_store.stop_session(runtime, entry)
    spawn_store.start_spawn(
        runtime,
        spawn_id="p1",
        chat_id=entry,
        harness="claude",
        kind="primary",
        harness_session_id="entry",
        model="",
        agent="",
        prompt="",
    )
    transcript = tmp_path / "native/exit.jsonl"
    publish = partial(
        session_store.get_or_create_observed_chat,
        runtime,
        entry,
        "claude",
        store,
        spawn_id="p1",
        native_exists=transcript.is_file,
    )

    def current() -> str | None:
        row = spawn_store.get_spawn(runtime, "p1")
        return row.current_chat_id if row is not None else None

    assert publish("exit") is None
    assert len(session_store.list_all_session_records(runtime)) == 1 and current() is None
    transcript.parent.mkdir()
    transcript.write_text('{"sessionId":"exit"}\n')
    exit_chat = publish("exit")
    assert exit_chat is not None and exit_chat != entry and current() == exit_chat
    record = session_store.chat_record(runtime, exit_chat)
    assert record is not None and record.stopped_at is not None
    assert publish("exit") == exit_chat
    transcript.unlink()
    assert publish("exit") is None  # Existing *other* chat must not bypass proof.
    assert publish("entry") is None  # Nor may the entry chat itself.
    assert current() == exit_chat
    assert len(session_store.list_all_session_records(runtime)) == 2


def test_observed_chat_is_protected_by_its_live_run_not_a_copied_lease(tmp_path: Path):
    runtime = tmp_path / "runtime"
    entry = session_store.reserve_chat_id(runtime)
    spawn_store.start_spawn(
        runtime,
        spawn_id="p1",
        chat_id=entry,
        harness="claude",
        kind="primary",
        model="",
        agent="",
        prompt="",
    )
    session_store.start_session(
        runtime,
        chat_id=entry,
        spawn_id="p1",
        harness="claude",
        model="",
        harness_session_id="entry",
        native_store=str(tmp_path / "native"),
    )
    try:
        current = session_store.get_or_create_observed_chat(
            runtime,
            entry,
            "claude",
            str(tmp_path / "native"),
            "current",
            spawn_id="p1",
        )
        assert not session_store.is_session_lease_owner_alive(runtime, current)
        assert session_identity.live_observer_for_chat(runtime, current) == "p1"
        with pytest.raises(ValueError, match="in use by live run p1"):
            session_store.start_session(
                runtime,
                chat_id=current,
                harness="claude",
                model="",
                harness_session_id="current",
                native_store=str(tmp_path / "native"),
            )
    finally:
        session_store.stop_session(runtime, entry)
    assert session_identity.live_observer_for_chat(runtime, current) is None
    session_store.start_session(
        runtime,
        chat_id=current,
        harness="claude",
        model="",
        harness_session_id="current",
        native_store=str(tmp_path / "native"),
    )
    session_store.stop_session(runtime, current)
