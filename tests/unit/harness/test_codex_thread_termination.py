import json
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, HarnessId, SpawnId
from meridian.lib.harness.common import extract_codex_report
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.semantics import PrimaryEventScope, normalize_event
from meridian.lib.state.artifact_store import LocalStore

MAIN_THREAD = "thread-main"
SUB_THREAD = "thread-sub"


def _codex_event(event_type: str, payload: dict[str, object]) -> RawHarnessEvent:
    return RawHarnessEvent(
        event_type=event_type,
        payload=payload,
        harness_id="codex",
    )


def _opencode_event(event_type: str, session_id: str | None) -> RawHarnessEvent:
    properties: dict[str, object] = {}
    if session_id is not None:
        properties["sessionID"] = session_id
    return RawHarnessEvent(
        event_type=event_type,
        payload={"type": event_type, "properties": properties},
        harness_id="opencode",
    )


def test_subagent_turn_completed_is_not_terminal_before_main_thread() -> None:
    started = _codex_event("turn/started", {"threadId": MAIN_THREAD, "turnId": "turn-main"})
    subagent_completed = _codex_event(
        "turn/completed", {"threadId": SUB_THREAD, "turnId": "turn-sub"}
    )
    main_completed = _codex_event(
        "turn/completed", {"threadId": MAIN_THREAD, "turnId": "turn-main"}
    )
    main_scope = PrimaryEventScope(HarnessId.CODEX, MAIN_THREAD, True)

    started_semantics = normalize_event(started, primary_event_scope=main_scope).semantics
    subagent_semantics = normalize_event(
        subagent_completed, primary_event_scope=main_scope
    ).semantics
    main_semantics = normalize_event(main_completed, primary_event_scope=main_scope).semantics

    assert started_semantics.terminal is None
    assert subagent_semantics.terminal is None
    assert subagent_semantics.clears_signal is False
    assert subagent_semantics.activity is None
    assert main_semantics.terminal is not None
    assert main_semantics.terminal.status == "succeeded"
    assert main_semantics.terminal.exit_code == 0
    assert main_semantics.clears_signal is True
    assert main_semantics.activity == "idle"


def test_single_thread_turn_completed_stays_terminal_without_thread_id() -> None:
    event = _codex_event("turn/completed", {"turnId": "turn-only"})
    outcome = normalize_event(event).semantics.terminal
    assert outcome is not None
    assert outcome.status == "succeeded"


def test_opencode_child_session_terminal_events_do_not_complete_parent_scope() -> None:
    parent_scope = PrimaryEventScope(HarnessId.OPENCODE, "ses_parent")
    child_idle = _opencode_event("session.idle", "ses_child")
    child_error = _opencode_event("session.error", "ses_child")
    parent_idle = _opencode_event("session.idle", "ses_parent")

    child_idle_semantics = normalize_event(
        child_idle, primary_event_scope=parent_scope
    ).semantics
    child_error_semantics = normalize_event(
        child_error, primary_event_scope=parent_scope
    ).semantics
    parent_semantics = normalize_event(parent_idle, primary_event_scope=parent_scope).semantics

    assert child_idle_semantics.terminal is None
    assert child_error_semantics.terminal is None
    assert child_idle_semantics.clears_signal is False
    assert child_idle_semantics.activity is None
    assert parent_semantics.terminal is not None
    assert parent_semantics.terminal.status == "succeeded"
    assert parent_semantics.clears_signal is True
    assert parent_semantics.activity == "idle"


def test_opencode_unscoped_terminal_event_only_counts_without_parent_scope() -> None:
    unscoped_idle = _opencode_event("session.idle", None)

    assert normalize_event(unscoped_idle).semantics.terminal is not None
    assert normalize_event(
        unscoped_idle,
        primary_event_scope=PrimaryEventScope(HarnessId.OPENCODE, "ses_parent"),
    ).semantics.terminal is None


def test_extract_codex_report_uses_main_thread_agent_message(tmp_path: Path) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-thread-report")
    records = [
        {
            "event_type": "turn/started",
            "payload": {"threadId": MAIN_THREAD, "turnId": "turn-main"},
        },
        {
            "event_type": "item/completed",
            "payload": {
                "threadId": SUB_THREAD,
                "item": {"type": "agentMessage", "text": "Subagent done."},
            },
        },
        {
            "event_type": "turn/completed",
            "payload": {"threadId": SUB_THREAD, "turnId": "turn-sub"},
        },
        {
            "event_type": "item/completed",
            "payload": {
                "threadId": MAIN_THREAD,
                "item": {"type": "agentMessage", "text": "Main thread done."},
            },
        },
        {
            "event_type": "turn/completed",
            "payload": {"threadId": MAIN_THREAD, "turnId": "turn-main"},
        },
    ]
    lines = "\n".join(json.dumps(record) for record in records)
    artifacts.put(ArtifactKey(f"{spawn_id}/history.jsonl"), f"{lines}\n".encode())

    assert extract_codex_report(artifacts, spawn_id) == "Main thread done."
