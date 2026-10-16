"""Generated replay golden, recorded from ae9a7c2a before the binding rewrite.

The digest covers complete serialized generation and chat records, not a subset
of fields. No real journal data is retained. Regenerate the reference only from
the baseline, never from the implementation under test.
"""

import hashlib
import json
import random
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from meridian.lib.state import session_fold
from meridian.lib.state import session_store as s
from tests.support.session_corpus import generated_session_events


def _dump(records: tuple[s.SessionRecord, ...]) -> list[dict[str, object]]:
    return [
        record.model_dump(mode="json", exclude={"launch_policy_snapshot"}) for record in records
    ]


def test_generated_histories_match_pre_restructure_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        results.append([_dump(baseline_generations), _dump(baseline_chats)])
    serialized = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(serialized).hexdigest() == (
        "f5c5954b1f3a2ecf2754d99cfe1e6733984c7e4128fc9c8c6fef017f79f9cc32"
    )


def test_later_start_only_closes_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for seed in range(25):
        rng = random.Random(seed)
        events: list[dict[str, object]] = []
        for ordinal in range(30):
            chat_id = rng.choice(["c1", "c2"])
            generation = f"g{ordinal}"
            events.append(
                {
                    "event": "start",
                    "chat_id": chat_id,
                    "harness": "codex",
                    "harness_session_id": f"native-{chat_id}",
                    "native_store": "/native",
                    "model": "test",
                    "started_at": f"start-{ordinal}",
                    "session_instance_id": generation,
                }
            )
            if rng.random() < 0.4:
                events.append(
                    {
                        "event": "stop",
                        "chat_id": chat_id,
                        "session_instance_id": generation,
                        "stopped_at": f"stop-{ordinal}",
                    }
                )
        (tmp_path / "sessions.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        projected = s.list_session_generations(tmp_path)
        projected_chats = s.list_all_session_records(tmp_path)
        with monkeypatch.context() as baseline_patch:
            baseline_patch.setattr(
                session_fold,
                "_close_abandoned_generations",
                lambda *_args: (),
            )
            baseline = s.list_session_generations(tmp_path)
            baseline_chats = s.list_all_session_records(tmp_path)

        assert len(projected) == len(baseline)
        for index, (before, after) in enumerate(zip(baseline, projected, strict=True)):
            later = next(
                (row for row in baseline[index + 1 :] if row.chat_id == before.chat_id),
                None,
            )
            should_close = before.stopped_at is None and later is not None
            expected = before.model_copy(
                update={"stopped_at": later.started_at} if should_close else {}
            )
            assert after == expected
        assert projected_chats == baseline_chats


def _spelling_variant_rows() -> list[str]:
    """Rows whose chat ID only decodes to its selected value; writers vary in spelling."""
    padded_history = s.SessionRecord(
        chat_id="c5",
        kind="spawn",
        harness="codex",
        harness_session_id="n5",
        model="test",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(),
        params=(),
        started_at="0",
        stopped_at="1",
        session_instance_id="historical-c5",
        record_mode="historical",
    ).model_dump(mode="json")
    padded_history["chat_id"] = " c5 "
    return [
        # Whitespace padding, default json.dumps spacing.
        json.dumps(
            {
                "event": "start",
                "chat_id": " c4 ",
                "harness": "codex",
                "harness_session_id": "n4",
                "model": "test",
                "started_at": "40",
                "session_instance_id": "g4",
            }
        ),
        # Escaped-control padding (\t), compact writer spacing.
        json.dumps(
            {"event": "update", "chat_id": "c4\t", "session_instance_id": "g4",
             "active_work_id": "tab"},
            separators=(",", ":"),
        ),
        # Spaced keys and an ensure_ascii \u-escaped no-break-space pad.
        '{"chat_id" : "\\u00a0c4" , "event" : "update" , "session_instance_id" : "g4",'
        ' "spawn_id" : "p4"}',
        # Raw UTF-8 ideographic-space pad (ensure_ascii=False writer).
        json.dumps(
            {"event": "update", "chat_id": "c4\u3000", "session_instance_id": "g4",
             "claude_config_dir": "/cfg"},
            ensure_ascii=False,
        ),
        # Escapes spelling the value and the key itself.
        '{"event":"update","chat_id":"c\\u0034","session_instance_id":"g4","native_store":"/n"}',
        '{"event":"update","chat\\u005fid":"c4","session_instance_id":"g4","spawn_id":"p44"}',
        # Duplicate key: the decoder keeps the last one.
        '{"event":"update","chat_id":"c2","chat_id":" c4 ","session_instance_id":"g4",'
        '"active_work_id":"dup"}',
        json.dumps(
            {"event": "update", "chat_id": " c1 ", "session_instance_id": "a",
             "active_work_id": "padded"}
        ),
        json.dumps({"event": "historical_import", "record": padded_history}),
        json.dumps(
            {"event": "model_observation", "harness": "codex", "harness_session_id": "n4",
             "observed_model_token": "m", "recorded_at": "0"}
        ),
    ]


def test_single_chat_replay_matches_full_replay(tmp_path: Path) -> None:
    """The byte prefilter may only nominate rows; selected replay must equal full replay."""
    variants = _spelling_variant_rows()
    for seed in range(200):
        lines = [json.dumps(event) for event in generated_session_events(seed)] + variants
        (tmp_path / "sessions.jsonl").write_text("".join(f"{line}\n" for line in lines))
        full = s._records_by_session(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert {"c4", "c5"} <= set(full)
        for chat_id in (*full, "c-absent"):
            selected = s._records_by_session(  # pyright: ignore[reportPrivateUsage]
                tmp_path, chat_ids={chat_id}
            )
            assert set(selected) <= {chat_id}
            assert selected.get(chat_id) == full.get(chat_id), (seed, chat_id)
