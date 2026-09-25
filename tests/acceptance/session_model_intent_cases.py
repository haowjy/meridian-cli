"""Fixed synthetic acceptance manifest, run only inside the guarded interpreter."""

import json
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from meridian.lib.state import session_authority as a
from meridian.lib.state import session_store as s
from tests.support.session_authority import append, pinned


def snapshot(root):
    result = s.read_native_source_use_snapshot(root)
    assert isinstance(result, s.NativeSourceUseSnapshot), result
    return result


def changed(event, **fields):
    return a.decode_row({**event.model_dump(), **fields})


def legacy(event, native="conversation", **fields):
    payload = event.model_dump()
    payload.pop("source")
    return a.decode_row({**payload, "v": 1, "harness_session_id": native, **fields})


def update(event, native):
    return a.SessionUpdateEvent(
        chat_id=event.chat_id,
        session_instance_id=event.session_instance_id,
        startup_attempt_id=event.startup_attempt_id,
        harness_session_id=native,
    )


def refused(call, reason=None):
    try:
        call()
    except ValueError as error:
        if reason:
            assert reason in str(error), (reason, str(error))
    else:
        raise AssertionError("expected refusal")


def parity_refusal(root, event, reason=None):
    # Both operations start at the same prefix, with a complete wire shape.
    a.decode_row(event.model_dump())
    before = (root / "sessions.jsonl").read_bytes()
    refused(lambda: s.record_model_selection(root, event), reason)
    assert (root / "sessions.jsonl").read_bytes() == before
    append(root, event)
    refused(lambda: a.read_journal((root / "sessions.jsonl").read_bytes()), reason)


def late_seed(root: Path) -> None:
    for startup in (None, "seed-start"):
        path = root / str(startup)
        _, _, event = pinned(path)
        assert s.record_model_selection(path, event)
        seed = changed(event, kind="initial_seed", startup_attempt_id=startup)
        parity_refusal(path, seed, "seed_after_intent")
    path = root / "seed-retry"
    source, _, event = pinned(path)
    seed = changed(event, kind="initial_seed", startup_attempt_id=None)
    assert s.record_model_selection(path, seed)
    assert s.record_model_selection(path, event)
    assert not s.record_model_selection(path, seed)
    assert snapshot(path).replay_model_facts(source).first_committed_seed is not None
    assert '"startup_attempt_id":null' in (path / "sessions.jsonl").read_text()
    append(path, seed)
    refused(lambda: a.read_journal((path / "sessions.jsonl").read_bytes()), "duplicate")


def startup_and_duplicate(root):
    for explicit in (None, "conversation"):
        path = root / str(explicit)
        _, _, event = pinned(path)
        append(path, update(event, "other"))
        parity_refusal(path, changed(event, harness_session_id=explicit), "startup attempt changed")
    path = root / "retry"
    _, _, event = pinned(path)
    assert s.record_model_selection(path, event)
    assert not s.record_model_selection(
        path, changed(event, startup_attempt_id="unseen-s2", recorded_at="earlier")
    )
    before = (path / "sessions.jsonl").read_bytes()
    append(path, update(changed(event, startup_attempt_id="contrary"), "other"))
    refused(
        lambda: s.record_model_selection(path, changed(event, startup_attempt_id="contrary")),
        "startup attempt changed",
    )
    assert len((path / "sessions.jsonl").read_bytes()) > len(before)
    for key in ("canonical_model_id", "provider_constraint", "provenance"):
        value = {"new": "provenance"} if key == "provenance" else "different"
        refused(
            lambda key=key, value=value: s.record_model_selection(
                path, changed(event, selection={**event.selection.model_dump(), key: value})
            ),
            "conflicting_intent",
        )
    append(path, event)
    refused(lambda: a.read_journal((path / "sessions.jsonl").read_bytes()), "duplicate")


def prefix_and_wire(root):
    for field, value in (
        ("v", True),
        ("v", False),
        ("v", 0),
        ("v", 3),
        ("v", "2"),
        ("v", 2.0),
        ("session_instance_id", ""),
        ("spawn_id", None),
        ("spawn_id", ""),
        ("harness_session_id", "wrong"),
    ):
        path = root / (field + repr(value))
        _, _, event = pinned(path)
        before = (path / "sessions.jsonl").read_bytes()
        # model_copy is deliberately unchecked; live writer must strictly normalize.
        refused(
            lambda path=path, event=event, field=field, value=value: s.record_model_selection(
                path, event.model_copy(update={field: value})
            )
        )
        assert (path / "sessions.jsonl").read_bytes() == before
        with (path / "sessions.jsonl").open("a") as handle:
            handle.write(json.dumps({**event.model_dump(), field: value}) + "\n")
        assert isinstance(s.read_native_source_use_snapshot(path), s.NativeIdUnavailable)
    for field, value in (
        ("session_instance_id", "wrong"),
        ("spawn_id", "wrong"),
        ("startup_attempt_id", "   "),
    ):
        path = root / field
        _, _, event = pinned(path)
        parity_refusal(path, changed(event, **{field: value}))
    for case in ("missing-start", "pre-pin", "protocol", "conflicting-start", "identical-start"):
        path = root / case
        source, start, event = pinned(path)
        if case in ("missing-start", "pre-pin"):
            rows = (path / "sessions.jsonl").read_text().splitlines(keepends=True)
            (path / "sessions.jsonl").write_text(
                "".join(rows[:2] if case == "missing-start" else rows[2:])
            )
        elif case == "protocol":
            # The captured prefix itself uses the new protocol.
            rows = (path / "sessions.jsonl").read_text().splitlines(keepends=True)
            (path / "sessions.jsonl").write_text("".join(rows[:2]))
            append(path, start.model_copy(update={"model_selection_protocol": 1}))
            event = changed(event, kind="initial_seed", startup_attempt_id=None)
        else:
            append(
                path,
                start
                if case == "identical-start"
                else start.model_copy(update={"model": "contradictory"}),
            )
        if case == "identical-start":
            assert s.record_model_selection(path, event)
            assert snapshot(path).replay_model_facts(source).latest_invocation
        else:
            parity_refusal(path, event)


def membership(builder):
    selections, invocations = Counter(), Counter()
    for entry in builder.metadata.model_intents:
        if entry.correlation != "ambiguous" and entry.effective_native_id is not None:
            key = (entry.value.harness, entry.effective_native_id)
            selections[key] += 1
            if entry.value.kind == "invocation_started":
                invocations[(*key, entry.value.spawn_id)] += 1
    assert dict(selections) == builder.metadata.selections
    assert dict(invocations) == builder.metadata.invocations


def builder_for(root):
    builder = a._JournalBuilder()
    for line in (root / "sessions.jsonl").read_text().splitlines():
        a.fold_row(builder, a.decode_row(json.loads(line)))
    return builder


def mixed_assertions(root):
    for exact in (False, True):
        path = root / str(exact)
        source, _, event = pinned(path)
        builder = builder_for(path)
        explicit = event if exact else legacy(event)
        rows = [explicit, legacy(event, None, spawn_id="deferred")]
        if exact:
            rows.append(legacy(event, "other", spawn_id="contrary"))
        rows += [
            update(event, "other" if exact else "conversation"),
            update(event, "conversation" if exact else "other"),
            update(event, "conversation"),
        ]
        for ordinal, row in enumerate(rows):
            a.fold_row(builder, row)
            membership(builder)
            if ordinal == 1:
                assert builder.metadata.model_intents[1].effective_native_id is None
            if ordinal == (3 if exact else 2):
                assert builder.metadata.model_intents[1].effective_native_id == (
                    "other" if exact else "conversation"
                )
            if exact and ordinal >= 2:
                assert builder.metadata.model_intents[0].correlation == "ambiguous"
                assert (
                    builder.metadata.exact_by_source[a.native_key_tuple(source.key)].blocked_by == 0
                )
        facts = builder.snapshot().metadata.model_intents
        assert facts[1].correlation == "ambiguous"
        assert facts[1].effective_native_id is None
        if not exact:
            assert facts[0].effective_native_id == "conversation"
        assert [fact.journal_ordinal for fact in facts] == sorted(
            fact.journal_ordinal for fact in facts
        )
        # Retaining ambiguous slots doesn't change public legacy reader semantics.
        for row in rows:
            append(path, row)
        result = s.get_model_selection(path, "pi", "other" if exact else "conversation")
        assert result is not None and result.canonical_model_id == "model"


def full_source_and_order(root):
    source, start, event = pinned(root)
    other, _, second = pinned(root, "c2", "/synthetic/other")
    second = changed(
        second, selection={**second.selection.model_dump(), "canonical_model_id": "second"}
    )
    assert s.record_model_selection(root, legacy(event))
    assert s.record_model_selection(root, event)
    assert s.record_model_selection(root, second)
    assert not s.record_model_selection(root, event)
    assert not s.record_model_selection(root, second)
    append(root, start.model_copy(update={"session_instance_id": "new", "spawn_id": "p2"}))
    latest = changed(
        event,
        session_instance_id="new",
        spawn_id="p2",
        recorded_at="0000",
        startup_attempt_id="new",
    )
    assert s.record_model_selection(root, latest)
    view = snapshot(root)
    assert view.replay_model_facts(source).latest_invocation.value.spawn_id == "p2"
    assert (
        view.replay_model_facts(other).latest_invocation.value.selection.canonical_model_id
        == "second"
    )
    for wrong in (
        source.model_copy(update={"ref": source.ref.model_copy(update={"chat_id": "c2"})}),
        source.model_copy(
            update={"ref": source.ref.model_copy(update={"locator_event_id": "0" * 64})}
        ),
        source.model_copy(
            update={"key": source.key.model_copy(update={"store": "/synthetic/other"})}
        ),
        source.model_copy(update={"locator": source.locator.model_copy(update={"path": "/wrong"})}),
    ):
        assert view.replay_model_facts(wrong) == a.FactsUnavailable("source_not_eligible")
        refused(
            lambda wrong=wrong: s.record_model_selection(
                root, event.model_copy(update={"source": wrong})
            )
        )
    assert view.replay_model_facts(source).original_seed_recovery == "origin_not_correlated"
    # An invalidated older generation blocks the source, not just that slot.
    append(root, start.model_copy(update={"model": "contradiction"}))
    assert snapshot(root).replay_model_facts(source) == a.FactsUnavailable("source_conflict")
    refused(lambda: s.record_model_selection(root, latest), "source_conflict")
    assert snapshot(root).replay_model_facts(other).latest_invocation


class CountedList(list):
    def __init__(self, values=()):
        super().__init__(values)
        self.visits = 0

    def __iter__(self):
        for value in super().__iter__():
            self.visits += 1
            yield value


def traversal_cost(root):
    for count in (100, 200):
        path = root / str(count)
        source, start, event = pinned(path)
        builder = builder_for(path)
        ordered = CountedList()
        builder.metadata.model_intents = ordered
        for index in range(count):
            a.fold_row(builder, legacy(event, None, spawn_id=f"p{index}"))
        watch = builder.metadata.startup_watch[a.startup_key(event)]
        deferred = CountedList(watch.deferred)
        watch.deferred = deferred
        a.fold_row(builder, legacy(event, "conversation", spawn_id="explicit"))
        a.fold_row(builder, update(event, "conversation"))
        assert deferred.visits == count and ordered.visits == 0
        a.fold_row(builder, update(event, "other"))
        assert deferred.visits == count * 2
        for native in ("conversation", "other", "third", "fourth"):
            a.fold_row(builder, update(event, native))
        assert deferred.visits == count * 2 and ordered.visits == 0
        builder.snapshot()
        assert ordered.visits == count + 1  # exactly one final freeze traversal
        print("deferred_visits", count, {"bind": count, "retract": count, "freeze": ordered.visits})
        # All exact entries share one startup but distinct captured generations/spawns.
        builder = builder_for(path)
        ordered = CountedList()
        builder.metadata.model_intents = ordered
        for index in range(count):
            spawn = f"p{index}"
            generation = f"g{index}"
            a.fold_row(
                builder,
                start.model_copy(update={"spawn_id": spawn, "session_instance_id": generation}),
            )
            a.fold_row(builder, changed(event, spawn_id=spawn, session_instance_id=generation))
        watched_event = changed(event, spawn_id="p0", session_instance_id="g0")
        watch = builder.metadata.startup_watch[a.startup_key(watched_event)]
        exact = CountedList(watch.exact)
        watch.exact = exact
        start_key = ("c1", "g0", "pi")
        starts = CountedList(builder.metadata.exact_by_start[start_key])
        builder.metadata.exact_by_start[start_key] = starts
        for index in range(count):
            a.fold_row(
                builder, update(changed(event, startup_attempt_id=f"unrelated-{index}"), "other")
            )
            a.fold_row(builder, update(watched_event, "conversation"))
        assert exact.visits == starts.visits == ordered.visits == 0
        a.fold_row(builder, legacy(watched_event, "other"))
        assert exact.visits == 1
        a.fold_row(builder, update(watched_event, "other"))
        contrary = start.model_copy(
            update={"session_instance_id": "g0", "spawn_id": "p0", "model": "contrary"}
        )
        a.fold_row(builder, contrary)
        a.fold_row(builder, contrary)
        assert starts.visits == 1 and exact.visits == 1 and ordered.visits == 0
        membership(builder)
        assert builder.metadata.exact_by_source[a.native_key_tuple(source.key)].blocked_by == 0
        print(
            "exact_visits",
            count,
            {"startup_conflict": exact.visits, "start_conflict": starts.visits},
        )


def immutable_and_cost(root):
    source, _, event = pinned(root)
    counts = Counter()
    decode, fold, read = a.decode_row, a.fold_row, s.read_journal
    freeze, admit = a._RetainedModelIntent.freeze, a.freeze_model_intent

    def counted(name, call):
        def invoke(*args, **kwargs):
            counts[name] += 1
            return call(*args, **kwargs)

        return invoke

    with (
        patch.object(a, "decode_row", counted("decode", decode)),
        patch.object(s, "decode_row", counted("proposal_decode", decode)),
        patch.object(a, "fold_row", counted("fold", fold)),
        patch.object(a, "freeze_model_intent", counted("admit", admit)),
        patch.object(a._RetainedModelIntent, "freeze", counted("snapshot_entry", freeze)),
        patch.object(s, "read_journal", counted("read", read)),
    ):
        assert s.record_model_selection(root, event)
        assert counts == {"decode": 3, "proposal_decode": 1, "fold": 3, "read": 1, "admit": 1}
        counts.clear()
        view = snapshot(root)
        assert counts == {"decode": 4, "fold": 4, "read": 1, "admit": 1, "snapshot_entry": 1}
        facts = view.replay_model_facts(source)
        entry = facts.latest_invocation
        event.selection.provenance["source"] = "caller-mutated"
        entry.event.selection.provenance["source"] = "view-mutated"
        try:
            entry.value.selection.provenance = frozenset()
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("retained payload mutable")
        for model, field in (
            (source, "key"),
            (source.key, "store"),
            (source.ref, "chat_id"),
            (source.locator, "path"),
            (source.locator.file_object, "inode"),
        ):
            refused(lambda model=model, field=field: setattr(model, field, None))
        try:
            view.journal.metadata.exact_by_source[a.native_key_tuple(source.key)].by_spawn["p2"] = 0
        except TypeError:
            pass
        else:
            raise AssertionError("index mutable")
        baseline = counts.copy()
        with (
            patch.object(Path, "open", side_effect=AssertionError("query opened file")),
            patch.object(
                a.BoundModelIntent,
                "event",
                property(lambda _: (_ for _ in ()).throw(AssertionError("query rebuilt event"))),
            ),
        ):
            for _ in range(200):
                assert view.replay_model_facts(source).latest_invocation is entry
        assert counts == baseline
        assert entry.event.selection.provenance == {"source": "fixture"}
        print("snapshot_cost", dict(counts), "queries", 200, "extra_work", 0)
    # Mutate the caller after the planner returns but before the durable leaf runs.
    path = root / "decided-payload"
    _, _, original = pinned(path)
    original = changed(original, kind="initial_seed", startup_attempt_id=None)
    leaf = s._append_accepted_model_selection

    def mutate_then_append(path, transaction, decision):
        original.selection.provenance["source"] = "racing-caller"
        leaf(path, transaction, decision)

    with patch.object(s, "_append_accepted_model_selection", mutate_then_append):
        assert s.record_model_selection(path, original)
    persisted = json.loads((path / "sessions.jsonl").read_text().splitlines()[-1])
    assert persisted["selection"]["provenance"] == {"source": "fixture"}
    assert persisted["startup_attempt_id"] is None


def run(root: Path) -> None:
    manifest = (
        late_seed,
        startup_and_duplicate,
        prefix_and_wire,
        mixed_assertions,
        full_source_and_order,
        traversal_cost,
        immutable_and_cost,
    )
    for case in manifest:
        case(root / case.__name__)
        print("PASS", case.__name__, flush=True)
    print("manifest_cases", len(manifest))
