"""Native keys agree across chat replay, generation replay and locked writes."""

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from meridian.lib.core.native_identity import NativeKey, NativeKeyFields
from meridian.lib.state import session_store as store
from meridian.lib.state.native_binding import Same


@pytest.mark.parametrize("generation", ["", "generation"])
@pytest.mark.parametrize("native_id", [None, "", "first"])
def test_replay_preserves_partial_keys_and_rejects_whole_conflicts(
    tmp_path: Path,
    generation: str,
    native_id: str | None,
) -> None:
    start = {
        "event": "start",
        "chat_id": "c1",
        "harness": "codex",
        "model": "test",
        "harness_session_id": native_id,
        "session_instance_id": generation,
        "started_at": "0",
    }
    events = [
        start,
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": "wrong",
            "harness_session_id": "wrong",
            "native_store": "/wrong",
        },
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": generation,
            "harness_session_id": "first",
            "startup_attempt_id": "attempt",
        },
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": generation,
            "native_store": "/native",
            "source": "legacy_import",
        },
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": generation,
            "harness_session_id": "second",
            "active_work_id": "rejected",
        },
        {**start, "started_at": "1", "session_instance_id": "next"},
    ]
    path = tmp_path / "sessions.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    with capture_logs() as logs:
        record = store.get_session_record(tmp_path, "c1")
        generations = store.list_session_generations(tmp_path)
    assert not logs
    assert record is not None
    assert record.native_key() == NativeKey("codex", "/native", "first")
    assert record.active_work_id is None
    assert len(generations) == 2
    assert all(row.native_key() == record.native_key() for row in generations)

    # The parser normalizes empty persisted IDs to None, but in-memory events
    # can still carry an explicit empty ID. Keep that projection behavior.
    rows = {"c1": record}
    event = store.SessionUpdateEvent.model_validate(
        {
            "chat_id": "c1",
            "session_instance_id": "next",
        }
    ).model_copy(update={"harness_session_id": ""})
    store.project_session_event(rows, event)
    assert rows["c1"].harness_session_id == ""
    assert rows["c1"].native_key() is None


def test_same_binding_appends_startup_attempt_link(tmp_path: Path) -> None:
    chat = store.start_session(tmp_path, "codex", "first", "test", native_store="/native")
    try:
        before = store.get_session_record(tmp_path, chat)
        assert before is not None
        path = tmp_path / "sessions.jsonl"
        count = len(path.read_text().splitlines())
        result = store.update_session_harness_id(
            tmp_path,
            chat,
            NativeKeyFields(session_id="first", native_store="/native"),
            session_instance_id=before.session_instance_id,
            startup_attempt_id="retry",
            source="observed",
        )
        assert isinstance(result, Same)
        lines = path.read_text().splitlines()
        assert len(lines) == count + 1
        assert json.loads(lines[-1])["startup_attempt_id"] == "retry"
        assert store.get_session_record(tmp_path, chat) == before
    finally:
        store.stop_session(tmp_path, chat)


def test_equivalent_generation_spelling_keeps_chat_key_for_later_conflicts(tmp_path: Path) -> None:
    start = {
        "event": "start",
        "chat_id": "c1",
        "harness": "codex",
        "model": "test",
        "harness_session_id": None,
        "session_instance_id": "a",
        "started_at": "0",
    }
    events = [
        start,
        {**start, "session_instance_id": " a ", "harness_session_id": "first"},
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": "a",
            "active_work_id": "accepted",
        },
        {
            "event": "update",
            "chat_id": "c1",
            "session_instance_id": "a",
            "harness_session_id": "other",
            "active_work_id": "rejected",
        },
    ]
    (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    record = store.get_session_record(tmp_path, "c1")
    assert record is not None
    assert record.harness_session_id == "first"
    generations = store.list_session_generations(tmp_path)
    assert [r.harness_session_id for r in generations] == [None, "first"]
    assert generations[0].active_work_id == "accepted"


def test_native_key_inversion_retains_aliases_and_omits_partial_bindings(tmp_path: Path) -> None:
    from meridian.lib.state.session_fold import by_native_key

    base = {
        "event": "start",
        "harness": "codex",
        "model": "test",
        "harness_session_id": "shared",
        "native_store": "/native",
        "started_at": "0",
    }
    events = [
        {**base, "chat_id": "c1"},
        {**base, "chat_id": "c2"},
        {**base, "chat_id": "c3", "native_store": None},
        {"event": "update", "chat_id": "c1", "harness_session_id": "rejected"},
    ]
    (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    records = {row.chat_id: row for row in store.list_all_session_records(tmp_path)}
    assert by_native_key(records) == {
        NativeKey("codex", "/native", "shared"): (records["c1"], records["c2"]),
    }
