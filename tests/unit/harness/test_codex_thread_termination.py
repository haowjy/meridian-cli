import json
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.common import extract_codex_report
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.semantics import (
    PrimaryEventScopeTracker,
    activity_transition,
    clears_signal,
    codex_primary_event_scope,
    opencode_primary_event_scope,
    terminal_outcome,
)
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
    tracker = PrimaryEventScopeTracker()
    started = _codex_event(
        "turn/started",
        {"threadId": MAIN_THREAD, "turnId": "turn-main"},
    )
    subagent_completed = _codex_event(
        "turn/completed",
        {"threadId": SUB_THREAD, "turnId": "turn-sub"},
    )
    main_completed = _codex_event(
        "turn/completed",
        {"threadId": MAIN_THREAD, "turnId": "turn-main"},
    )

    main_scope = codex_primary_event_scope(MAIN_THREAD)

    assert tracker.terminal_outcome(started) is None
    assert tracker.primary_event_scope == main_scope
    assert tracker.terminal_outcome(subagent_completed) is None
    assert terminal_outcome(subagent_completed, primary_event_scope=main_scope) is None
    assert clears_signal(subagent_completed, primary_event_scope=main_scope) is False
    assert activity_transition(subagent_completed, primary_event_scope=main_scope) is None

    outcome = tracker.terminal_outcome(main_completed)
    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert clears_signal(main_completed, primary_event_scope=main_scope) is True
    assert activity_transition(main_completed, primary_event_scope=main_scope) == "idle"


def test_single_thread_turn_completed_stays_terminal_without_thread_id() -> None:
    event = _codex_event("turn/completed", {"turnId": "turn-only"})
    outcome = terminal_outcome(event)
    assert outcome is not None
    assert outcome.status == "succeeded"


def test_opencode_child_session_terminal_events_do_not_complete_parent_scope() -> None:
    parent_scope = opencode_primary_event_scope("ses_parent")
    child_idle = _opencode_event("session.idle", "ses_child")
    child_error = _opencode_event("session.error", "ses_child")
    parent_idle = _opencode_event("session.idle", "ses_parent")

    assert terminal_outcome(child_idle, primary_event_scope=parent_scope) is None
    assert terminal_outcome(child_error, primary_event_scope=parent_scope) is None
    assert clears_signal(child_idle, primary_event_scope=parent_scope) is False
    assert activity_transition(child_idle, primary_event_scope=parent_scope) is None

    outcome = terminal_outcome(parent_idle, primary_event_scope=parent_scope)
    assert outcome is not None
    assert outcome.status == "succeeded"
    assert clears_signal(parent_idle, primary_event_scope=parent_scope) is True
    assert activity_transition(parent_idle, primary_event_scope=parent_scope) == "idle"


def test_opencode_unscoped_terminal_event_only_counts_without_parent_scope() -> None:
    unscoped_idle = _opencode_event("session.idle", None)

    assert terminal_outcome(unscoped_idle) is not None
    assert terminal_outcome(
        unscoped_idle,
        primary_event_scope=opencode_primary_event_scope("ses_parent"),
    ) is None


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
