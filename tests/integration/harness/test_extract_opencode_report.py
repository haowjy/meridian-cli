"""OpenCode reports are attempt-owned; ambient stores and later turns cannot win."""

from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.extractors.opencode import OPENCODE_EXTRACTOR
from meridian.lib.launch.extract import enrich_finalize
from meridian.lib.state.artifact_store import InMemoryStore
from tests.support.opencode_db import write_opencode_v2_db_session


def test_extract_opencode_report_ignores_child_session_assistant_text():
    facts = AttemptFacts()
    for role, session, message in [
        ("user", "ses_parent", "u"),
        ("assistant", "ses_child", "child"),
        ("assistant", "ses_parent", "parent"),
    ]:
        OPENCODE_EXTRACTOR.fold(
            facts,
            {
                "type": "message.updated",
                "properties": {"info": {"role": role, "sessionID": session, "id": message}},
            },
        )
        OPENCODE_EXTRACTOR.fold(
            facts,
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "sessionID": session,
                        "messageID": message,
                        "type": "text",
                        "text": message,
                    }
                },
            },
        )
    assert facts.first_session_id == "ses_parent"
    assert facts.final_text == "parent"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "server.connected", "id": "evt_not_a_session"},
        {"type": "custom", "session_id": "evt_not_a_session"},
    ],
)
def test_extract_session_id_rejects_non_session_value(event):
    facts = AttemptFacts()
    OPENCODE_EXTRACTOR.fold(facts, event)
    assert facts.first_session_id is None


def test_extract_opencode_report_reads_v2_db_final_assistant(tmp_path: Path, monkeypatch):
    """assistantMessageID joins session_message.id, in the recorded store only."""
    recorded, ambient = tmp_path / "recorded.db", tmp_path / "ambient.db"
    session = "ses_v2"
    for db, text in [(recorded, "owned native reply"), (ambient, "WRONG STORE")]:
        write_opencode_v2_db_session(
            db_path=db,
            session_id=session,
            messages=[
                ("user", {"text": "question"}),
                ("assistant", {"content": [{"type": "text", "text": text}]}),
                ("assistant", {"content": [{"type": "text", "text": "LATER UNOWNED TURN"}]}),
            ],
        )
    monkeypatch.setenv("OPENCODE_DB", str(ambient))
    facts = AttemptFacts()
    OPENCODE_EXTRACTOR.fold(
        facts,
        {
            "type": "session.text.ended",
            "sessionID": session,
            "assistantMessageID": f"{session}_msg_1",
            "text": "stream fallback",
        },
    )
    assert facts.native_turn_ids == (f"{session}_msg_1",)
    extraction = enrich_finalize(
        facts=facts,
        extractor=OPENCODE_EXTRACTOR,
        native_key=NativeKey("opencode", str(recorded), session),
        artifacts=InMemoryStore(),
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path / "run",
    )
    assert extraction.report.content == "owned native reply"
    assert extraction.harness_session_id == session


@pytest.mark.parametrize("ids", [(), ("missing",), ("ses_other_msg_0",)])
def test_no_native_report_without_exact_attribution(tmp_path: Path, ids):
    db = tmp_path / "native.db"
    for session in ["ses_owned", "ses_other"]:
        write_opencode_v2_db_session(
            db_path=db,
            session_id=session,
            messages=[
                ("assistant", {"content": [{"type": "text", "text": "must not leak"}]}),
            ],
        )
    assert (
        OPENCODE_EXTRACTOR.read_native_turn(NativeKey("opencode", str(db), "ses_owned"), ids)
        is None
    )


def test_v2_missing_join_falls_back_to_stream(tmp_path: Path):
    facts = AttemptFacts()
    OPENCODE_EXTRACTOR.fold(
        facts,
        {
            "type": "session.text.ended",
            "sessionID": "ses_owned",
            "assistantMessageID": "missing",
            "text": "streamed reply",
        },
    )
    extraction = enrich_finalize(
        facts=facts,
        extractor=OPENCODE_EXTRACTOR,
        native_key=NativeKey("opencode", str(tmp_path / "absent.db"), "ses_owned"),
        artifacts=InMemoryStore(),
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
    )
    assert extraction.report.content == "streamed reply"


@pytest.mark.parametrize("explicit", [False, True])
def test_unreadable_native_db_cannot_break_report_precedence(tmp_path: Path, explicit):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not sqlite")
    if explicit:
        (tmp_path / "report.md").write_text("explicit report")
    facts = AttemptFacts(final_text="stream report", native_turn_ids=("owned",))
    result = enrich_finalize(
        facts=facts,
        extractor=OPENCODE_EXTRACTOR,
        native_key=NativeKey("opencode", str(db), "ses_owned"),
        artifacts=InMemoryStore(),
        spawn_id=SpawnId("p1"),
        log_dir=tmp_path,
    )
    assert result.report.content == ("explicit report" if explicit else "stream report")


def test_child_events_before_parent_user_do_not_supply_run_facts():
    facts = AttemptFacts(scope_session_id="ses_parent")
    OPENCODE_EXTRACTOR.fold(
        facts,
        {
            "type": "session.text.ended",
            "sessionID": "ses_child",
            "assistantMessageID": "child",
            "text": "child reply",
        },
    )
    assert facts.first_session_id is None
    assert facts.final_text is None
    assert facts.native_turn_ids == ()
    OPENCODE_EXTRACTOR.fold(
        facts,
        {
            "type": "session.text.ended",
            "sessionID": "ses_parent",
            "assistantMessageID": "parent",
            "text": "parent reply",
        },
    )
    assert facts.first_session_id == "ses_parent"
    assert facts.final_text == "parent reply"
    assert facts.native_turn_ids == ("parent",)
