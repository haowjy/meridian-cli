"""Frozen global-order lookup comparisons, with optional real-corpus I/O capture.

Run: uv run python -m tests.support.session_lookup_equivalence /tmp/copies /tmp/writer
Copies must be private. Lookup data stays unchanged; decision locks use writer.
Both synthetic and real-corpus modes exercise the real candidate disk seams.
Only the frozen oracle is memoized. --limit selects a deterministic sample for
large logs; omit it for exhaustive (potentially hours-long) disk comparisons.
"""

import json
import time
from collections import defaultdict
from pathlib import Path

import pytest

from meridian.lib.state import session_identity as identity
from meridian.lib.state import session_store as s
from meridian.lib.state import spawn_store
from meridian.lib.state.event_store import read_events
from meridian.lib.state.session_fold import (
    UnsupportedSessionRecords,
    fold_session_generations,
    parse_event,
    project_session_event,
)
from tests.support import session_lookup_oracle as old


def outcome(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        return (type(exc).__name__, str(exc))


def best_native_generation(root: Path, native: str, *, harness: str | None = None):
    matches = s.native_generations(root, native, harness=harness)
    return matches[-1] if matches else None


def oracle_pick(root: Path, native: str, *, harness: str | None = None):
    """The old resolver's newest generation, except that a newest one with no native
    store yields to an older one that has one: the rule resume and read now share."""
    picked = old.resolve_session_ref(root, native, harness=harness)
    if picked is not None and picked.native_key() is None:
        keyed = [
            record
            for record in s.native_generations(root, native, harness=harness)
            if record.native_key() is not None
        ]
        if keyed:
            return keyed[-1]
    return picked


def compare_reads(root: Path, chats: list[str], identities: set[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chat in chats:
        assert outcome(s.chat_record, root, chat) == outcome(old.get_session_record, root, chat), (
            chat
        )
    counts["chat_record"] = len(chats)
    for name in ("get_model_selection", "get_initial_model_selection", "get_last_executed_model"):
        for harness, native in sorted(identities):
            actual = outcome(getattr(s, name), root, harness, native)
            expected = outcome(getattr(old, name), root, harness, native)
            assert actual == expected, (name, harness, native, actual, expected)
        counts[name] = len(identities)
    for harness, native in sorted(identities):
        assert outcome(best_native_generation, root, native, harness=harness) == outcome(
            oracle_pick, root, native, harness=harness
        ), ("resolve_session_ref", harness, native)
    for native in sorted({native for _, native in identities}):
        assert outcome(best_native_generation, root, native) == outcome(oracle_pick, root, native)
    counts["resolve_session_ref"] = len(identities) + len({native for _, native in identities})
    return counts


def sample(items: list, limit: int | None) -> list:
    """Spread bounded disk checks across the corpus instead of only its oldest prefix."""
    if limit is None:
        return items
    if limit < 1:
        raise ValueError("Sample limit must be positive")
    return items[:: max(1, len(items) // limit)][:limit]


def compare_corpus(root: Path, writer: Path, *, limit: int | None = None) -> dict[str, int]:
    raw = read_events(root / "sessions.jsonl", lambda row: row)
    events = [
        event for row in raw if (event := parse_event(row, UnsupportedSessionRecords())) is not None
    ]
    observations = [
        event for row in raw if (event := old._parse_model_observation(row)) is not None
    ]
    by_chat = defaultdict(list)
    records = {}
    for event in events:
        by_chat[event.chat_id].append(event)
        project_session_event(records, event)
    generations = fold_session_generations(events)
    spawns = spawn_store.list_spawns(root).records
    native_keys = {(r.harness, r.harness_session_id) for r in generations if r.harness_session_id}
    native_keys |= {(r.harness, r.harness_session_id) for r in observations}
    chat_harnesses = defaultdict(set)
    for row in raw:
        record = row.get("record", {}) if row.get("event") == "historical_import" else row
        if record.get("harness"):
            chat_harnesses[record.get("chat_id")].add(record["harness"])
    for row in raw:
        record = row.get("record", {}) if row.get("event") == "historical_import" else row
        harnesses = (
            {record["harness"]} if record.get("harness") else chat_harnesses[record.get("chat_id")]
        )
        raw_keys = [record.get("harness_session_id"), *record.get("harness_session_ids", [])]
        native_keys.update(
            (harness, key.strip())
            for harness in harnesses
            for key in raw_keys
            if isinstance(key, str) and key.strip()
        )
    counts = {}
    with pytest.MonkeyPatch.context() as mp:
        # Only the oracle is captured. Every candidate read goes to disk.
        mp.setattr(
            old,
            "read_events",
            lambda path, parser, **kw: (
                observations if parser is old._parse_model_observation else events
            ),
        )
        mp.setattr(old, "_records_by_session", lambda *_args, **_kw: records)
        mp.setattr(old, "list_session_generations", lambda _root: generations)
        mp.setattr(s, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
        mp.setattr(old, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
        counts = compare_reads(
            root, sample(sorted(by_chat), limit), set(sample(sorted(native_keys), limit))
        )
        actual = outcome(identity.session_records_for_spawns, root, spawns)
        expected = outcome(old.session_records_for_spawns, root, spawns)
        assert actual == expected, ("spawns", root.name, actual, expected)
        counts["session_records_for_spawns"] = len(spawns)
        live = [r for r in records.values() if r.record_mode == "live"]
        keys = {
            (r.harness, r.native_store, r.harness_session_id)
            for r in live
            if r.native_store and r.harness_session_id
        }
        keys = sample(sorted(keys), limit)
        for index, (harness, store, native) in enumerate(keys):
            source = live[index % len(live)].chat_id
            kwargs = dict(harness=harness, native_store=store, session_id=native)
            actual = s._get_or_create_observed_chat_locked(root, source, **kwargs)
            expected = old._get_or_create_observed_chat_locked(root, source, **kwargs)
            assert actual == expected, ("observed", root.name, source, kwargs, actual, expected)
        counts["observed_chat"] = len(keys)
        writer.mkdir(parents=True, exist_ok=True)
        selections = sample(
            [e for e in events if isinstance(e, s.SessionModelSelectionEvent)], limit
        )
        for event in selections:
            actual_rows = []
            expected_rows = []
            with mp.context() as writes:
                writes.setattr(
                    old,
                    "append_event",
                    lambda _p, _l, event, output=expected_rows, **kw: output.append(event),
                )
                writes.setattr(
                    s,
                    "append_chat_rows",
                    lambda _root, batches, output=actual_rows: output.extend(
                        event for rows in batches.values() for event in rows
                    ),
                )
                actual = outcome(s.record_model_selection, root, event)
                expected = outcome(old.record_model_selection, writer, event)
            assert (actual, actual_rows) == (expected, expected_rows), (
                "decision",
                root.name,
                event,
                actual,
                expected,
            )
        counts["record_model_selection"] = len(selections)
    return counts


def compare_selection_appends(root: Path, writer: Path) -> dict[str, int]:
    """Real disk appends: first invocation, then same invocation on another alias."""
    import shutil

    records = old._records_by_session(root)
    groups = defaultdict(list)
    for record in records.values():
        if (
            record.record_mode == "live"
            and record.harness_session_id
            and record.session_instance_id
        ):
            groups[(record.harness, record.harness_session_id)].append(record)
    aliases = max(groups.values(), key=len)
    assert len(aliases) >= 2
    roots = [writer / "candidate", writer / "oracle"]
    for path in roots:
        path.mkdir(parents=True)
        shutil.copyfile(root / "sessions.jsonl", path / "sessions.jsonl")
    for index, record in enumerate(aliases[:2]):
        event = s.SessionModelSelectionEvent(
            kind="invocation_started",
            harness=record.harness,
            harness_session_id=record.harness_session_id,
            chat_id=record.chat_id,
            session_instance_id=record.session_instance_id,
            spawn_id="p900001",
            startup_attempt_id=f"review-first-{index}",
            recorded_at="2026-10-16T00:00:00Z",
            selection=s.ConversationModelSelection(
                selection_source="unknown",
                model_mode="harness_default",
                requested_token="review-first-selection",
            ),
        )
        actual = s.record_model_selection(roots[0], event)
        expected = old.record_model_selection(roots[1], event)
        assert actual == expected == (index == 0)
        assert [
            json.loads(line) for line in (roots[0] / "sessions.jsonl").read_text().splitlines()
        ] == [json.loads(line) for line in (roots[1] / "sessions.jsonl").read_text().splitlines()]
    return {"first_time_append": 1, "cross_chat_dedup": 1}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("copies", type=Path)
    parser.add_argument("writer", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    copies, writer = args.copies, args.writer
    for directory in (copies, writer):
        if not directory.resolve().is_relative_to("/tmp"):
            raise ValueError("Use private /tmp copies and writer directory")
    for root in sorted(copies.iterdir()):
        if not (root / "sessions.jsonl").is_file():
            continue
        started = time.monotonic()
        counts = compare_corpus(root, writer, limit=args.limit)
        counts.update(compare_selection_appends(root, writer / root.name))
        print(
            json.dumps(
                dict(runtime=root.name, counts=counts, seconds=round(time.monotonic() - started, 2))
            ),
            flush=True,
        )
