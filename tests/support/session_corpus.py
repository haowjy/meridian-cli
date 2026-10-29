"""Deterministic adversarial session rows shared by fold and lookup contracts."""

import hashlib
import json
import random
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from meridian.lib.state import session_fold
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



GOLDEN_FOLD_DIGEST = "f5c5954b1f3a2ecf2754d99cfe1e6733984c7e4128fc9c8c6fef017f79f9cc32"


def assert_pre_restructure_fold_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay 1,000 deterministic journals against the fold recorded before the binding rewrite.

    The digest covers the baseline serialized generation and chat fields. New metadata
    absent from that baseline is excluded; its reference digest stays fixed. No real
    journal data is retained. Regenerate the reference only from the baseline at
    ``ae9a7c2a``, never from the implementation under test.
    """
    results = []
    for seed in range(1000):
        events = generated_session_events(seed)
        (tmp_path / "sessions.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        with capture_logs() as logs, monkeypatch.context() as baseline_patch:
            baseline_patch.setattr(
                session_fold,
                "_close_abandoned_generations",
                lambda *_args: (),
            )
            baseline_generations = s.list_session_generations(tmp_path)
            baseline_chats = s.list_all_session_records(tmp_path)
        assert not logs
        results.append(
            [_golden_dump(baseline_generations), _golden_dump(baseline_chats)]
        )
    serialized = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == GOLDEN_FOLD_DIGEST


def _golden_dump(records: tuple[s.SessionRecord, ...]) -> list[dict[str, object]]:
    return [
        record.model_dump(
            mode="json",
            exclude={
                "launch_policy_snapshot",
                "created_at",
                "tmux_target",
                "context_from_chat_id",
                "context_from_history_id",
            },
        )
        for record in records
    ]
