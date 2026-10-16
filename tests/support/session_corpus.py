"""Deterministic adversarial session rows shared by fold and lookup contracts."""

import random

from meridian.lib.state import session_store as s


def generated_session_events(seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    events: list[dict[str, object]] = []
    for i in range(35):
        event = rng.choice(["start", "start", "update", "update", "stop"])
        row: dict[str, object] = {
            "event": event,
            "chat_id": rng.choice(["c1", "c2"]),
            "session_instance_id": rng.choice(["", "a", "b", " a "]),
        }
        if event == "start":
            row.update(
                harness=rng.choice(["claude", "codex"]),
                harness_session_id=rng.choice(["", "id1", "id2", None]),
                native_store=rng.choice([None, "", "/a", "/b"]),
                model="test",
                started_at=str(i),
            )
        elif event == "update":
            row.update(
                harness_session_id=rng.choice(["", "id1", "id2", None]),
                native_store=rng.choice([None, "", "/a", "/b"]),
                active_work_id="work",
                source=rng.choice(["legacy_import", "observed"]),
            )
        events.append(row)
    historical = s.SessionRecord(
        chat_id="c3",
        kind="spawn",
        harness="codex",
        harness_session_id="historical",
        model="test",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(),
        params=(),
        started_at="0",
        stopped_at="1",
        session_instance_id="historical-old",
        record_mode="historical",
    )
    events.extend(
        [
            {"event": "historical_import", "record": historical.model_dump(mode="json")},
            {
                "event": "start",
                "chat_id": "c3",
                "harness": "codex",
                "harness_session_id": "historical",
                "model": "test",
                "started_at": "2",
                "session_instance_id": "new",
            },
            {
                "event": "update",
                "chat_id": "c3",
                "session_instance_id": "new",
                "native_store": "/import",
                "source": "legacy_import",
            },
            {
                "event": "update",
                "chat_id": "c3",
                "session_instance_id": "new",
                "harness_session_id": "historical",
                "startup_attempt_id": "retry",
            },
        ]
    )
    return events

