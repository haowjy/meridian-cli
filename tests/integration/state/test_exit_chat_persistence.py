"""A native transcript must still exist when an exit chat is reused."""

from functools import partial
from pathlib import Path

from meridian.lib.state import session_store


def test_exit_chat_requires_persistence_on_creation_and_reuse(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    store = str(tmp_path / "native")
    entry = session_store.start_session(
        runtime, harness="claude", harness_session_id="entry", model="", native_store=store,
    )
    session_store.stop_session(runtime, entry)
    transcript = tmp_path / "native/exit.jsonl"
    allocate = partial(
        session_store.get_or_create_exit_chat, runtime, entry, "claude", store, "exit",
        native_exists=transcript.is_file,
    )
    assert allocate() is None
    assert len(session_store.list_all_session_records(runtime)) == 1
    transcript.parent.mkdir()
    transcript.write_text('{"sessionId":"exit"}\n')
    exit_chat = allocate()
    assert exit_chat is not None and exit_chat != entry
    record = session_store.get_session_record(runtime, exit_chat)
    assert record is not None and record.stopped_at is not None
    assert allocate() == exit_chat
    assert session_store.get_or_create_exit_chat(
        runtime, exit_chat, "claude", store, "exit", native_exists=transcript.is_file,
    ) == exit_chat
    transcript.unlink()
    assert allocate() is None  # Existing *other* chat must not bypass proof.
    assert session_store.get_or_create_exit_chat(
        runtime, exit_chat, "claude", store, "exit", native_exists=transcript.is_file,
    ) is None  # Nor may the entry chat itself.
    assert len(session_store.list_all_session_records(runtime)) == 2
