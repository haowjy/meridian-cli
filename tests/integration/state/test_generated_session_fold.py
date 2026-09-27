"""Generated replay golden, recorded from ae9a7c2a before the binding rewrite.

The digest covers complete serialized generation and chat records, not a subset
of fields. No real journal data is retained. Regenerate the reference only from
the baseline, never from the implementation under test.
"""

import hashlib
import json
import random
from pathlib import Path

from structlog.testing import capture_logs

from meridian.lib.state import session_store as s


def test_generated_histories_match_pre_restructure_fold(tmp_path: Path) -> None:
    results = []
    for seed in range(1000):
        rng = random.Random(seed)
        events = []
        for i in range(35):
            event = rng.choice(["start", "start", "update", "update", "stop"])
            row = {
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
        # An inert historical chat then a new generation; store-only and attempt links.
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
            session_instance_id="old",
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
        (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        with capture_logs() as logs:
            result = [
                [r.model_dump(mode="json") for r in f(tmp_path)]
                for f in (s.list_session_generations, s.list_all_session_records)
            ]
        assert not logs
        results.append(result)
    serialized = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == (
        "aab97638816a5ea383e3549200df3872e37aa485dc68292ee6effb5494cf0fc4"
    )
