"""Frozen global-order lookup comparisons, with optional real-corpus I/O capture.

Run: uv run python -m tests.support.session_lookup_equivalence /tmp/copies /tmp/writer
Copies must be private. Lookup data stays unchanged; decision locks use writer.
Synthetic tests exercise disk seams. For large real corpora, capture I/O once:
frozen functions retain global order; candidate functions receive chat/native
partitions. Only already-evaluated pure full-fold/binding results are memoized.
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


def compare_reads(root: Path, chats: list[str], identities: set[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chat in chats:
        assert outcome(s.get_session_record, root, chat) == outcome(
            old.get_session_record, root, chat
        ), chat
    counts["get_session_record"] = len(chats)
    for name in ("get_model_selection", "get_initial_model_selection", "get_last_executed_model"):
        for harness, native in sorted(identities):
            actual = outcome(getattr(s, name), root, harness, native)
            expected = outcome(getattr(old, name), root, harness, native)
            assert actual == expected, (name, harness, native, actual, expected)
        counts[name] = len(identities)
    for harness, native in sorted(identities):
        assert outcome(s.resolve_session_ref, root, native, harness=harness) == outcome(
            old.resolve_session_ref, root, native, harness=harness
        ), ("resolve_session_ref", harness, native)
    for native in sorted({native for _, native in identities}):
        assert outcome(s.resolve_session_ref, root, native) == outcome(
            old.resolve_session_ref, root, native
        )
    counts["resolve_session_ref"] = len(identities) + len({native for _, native in identities})
    return counts


def compare_corpus(root: Path, writer: Path) -> dict[str, int]:
    raw = read_events(root / "sessions.jsonl", lambda row: row)
    events = [event for row in raw if (event := parse_event(row)) is not None]
    observations = [
        event for row in raw if (event := old._parse_model_observation(row)) is not None
    ]
    by_chat = defaultdict(list)
    by_native = defaultdict(set)
    obs = defaultdict(list)
    records = {}
    for event in events:
        by_chat[event.chat_id].append(event)
        project_session_event(records, event)
    for row in raw:
        record = row.get("record", {}) if row.get("event") == "historical_import" else row
        chat = record.get("chat_id")
        if isinstance(chat, str) and chat.strip():
            for native in s.native_ids(row):
                by_native[native].add(chat.strip())
    for event in observations:
        obs[event.harness_session_id].append(event)
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
        native_keys.update((harness, key) for harness in harnesses for key in s.native_ids(row))
    bound_full = old._bound_model_selections(events)
    original_bound = old._bound_model_selections
    counts = {}
    with pytest.MonkeyPatch.context() as mp:
        # Freeze I/O once. Old algorithms keep global order; candidate seams
        # receive independent partitions from raw claims. No lookup is rewritten.
        mp.setattr(
            old,
            "read_events",
            lambda path, parser, **kw: (
                observations if parser is old._parse_model_observation else events
            ),
        )
        mp.setattr(old, "_records_by_session", lambda *_args, **_kw: records)
        mp.setattr(old, "list_session_generations", lambda _root: generations)
        mp.setattr(
            old,
            "_bound_model_selections",
            lambda rows: bound_full if rows is events else original_bound(rows),
        )
        mp.setattr(s, "chat_rows", lambda _root, chat: list(by_chat[chat]))
        mp.setattr(s, "native_chats", lambda _root, native: tuple(by_native[native]))
        mp.setattr(s, "native_observations", lambda _root, native: obs[native])
        mp.setattr(s, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
        mp.setattr(old, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
        counts = compare_reads(root, sorted(by_chat), native_keys)
        for row in spawns:
            actual = outcome(identity.session_records_for_spawns, root, [row])
            expected = outcome(old.session_records_for_spawns, root, [row])
            assert actual == expected, ("spawn", root.name, row.id, actual, expected)
        counts["session_records_for_spawns"] = len(spawns)
        live = [r for r in records.values() if r.record_mode == "live"]
        keys = {
            (r.harness, r.native_store, r.harness_session_id)
            for r in live
            if r.native_store and r.harness_session_id
        }
        for harness, store, native in keys:
            source = live[0].chat_id
            kwargs = dict(harness=harness, native_store=store, session_id=native)
            actual = s._get_or_create_observed_chat_locked(root, source, **kwargs)
            expected = old._get_or_create_observed_chat_locked(root, source, **kwargs)
            assert actual == expected, ("observed", root.name, source, kwargs, actual, expected)
        counts["observed_chat"] = len(keys)
        writer.mkdir(parents=True, exist_ok=True)
        selections = [e for e in events if isinstance(e, s.SessionModelSelectionEvent)]
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
                    "_append_session_event",
                    lambda _p, event, output=actual_rows, **kw: output.append(event),
                )
                actual = outcome(s.record_model_selection, writer, event)
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


if __name__ == "__main__":
    import sys

    copies, writer = map(Path, sys.argv[1:])
    for directory in (copies, writer):
        if not directory.resolve().is_relative_to("/tmp"):
            raise ValueError("Use private /tmp copies and writer directory")
    for root in sorted(copies.iterdir()):
        if not (root / "sessions.jsonl").is_file():
            continue
        started = time.monotonic()
        counts = compare_corpus(root, writer)
        print(
            json.dumps(
                dict(runtime=root.name, counts=counts, seconds=round(time.monotonic() - started, 2))
            ),
            flush=True,
        )
