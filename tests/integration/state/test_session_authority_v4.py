"""Pure v4 journal codec/reducer coverage; no filesystem/native harness reads."""

from __future__ import annotations

import json

import pytest

from meridian.lib.state import session_authority as authority
from meridian.lib.state import session_store


def _key(store: str = "/native/store", session_id: str = "session") -> authority.NativeSessionKey:
    return authority.NativeSessionKey(harness="pi", store=store, native_session_id=session_id)


def _begin(run_id: str = "run", attempt_id: str = "attempt") -> authority.BeginEventV4:
    return authority.BeginEventV4(
        run_id=run_id,
        attempt_id=attempt_id,
        transport_scope_id="transport",
        harness="pi",
        store="/native/store",
        operation="fresh",
        attempt_number=1,
    )


def _file(path: str, *, inode: int, file_inode: int | None = None) -> authority.QualifiedLocalFile:
    return authority.QualifiedLocalFile(
        kind="local_file",
        path=path,
        store_object={"device": 1, "inode": inode},
        file_object={"device": 1, "inode": file_inode if file_inode is not None else inode + 1},
        rule="pi-session-file:v1",
    )


def _fact(
    boundary: str,
    observation: authority.NativeFileObservation,
    *,
    order: int = 1,
    session_id: str = "session",
) -> authority.BoundaryFactV4:
    entry = boundary == "entry"
    evidence = authority.BoundaryEvidence(
        transport_scope_id="transport",
        order=order,
        correlation=f"corr-{order}",
        selection=authority.CreatedSelection(creation_request="created") if entry else None,
        terminal_rule=None if entry else "terminal:v1",
    )
    return authority.BoundaryFactV4(
        run_id="run",
        attempt_id="attempt",
        boundary=boundary,
        key=_key(session_id=session_id),
        evidence=evidence,
        file=observation,
    )


def _fold(*events: authority.V4AttemptEvent) -> authority._JournalBuilder:
    builder = authority._JournalBuilder()
    for event in events:
        authority.fold_row(builder, event)
    return builder


def _accept(
    builder: authority._JournalBuilder,
    fact: authority.BoundaryFactV4,
    chat_id: str | None = None,
) -> authority.V4AttemptEvent:
    result = authority.plan_attempt_v4(
        builder.attempts,
        builder.latest,
        builder.identity(),
        fact,
        assigned_chat=chat_id,
    )
    if isinstance(result, authority.NeedChat):
        result = authority.plan_attempt_v4(
            builder.attempts,
            builder.latest,
            builder.identity(),
            fact,
            assigned_chat=chat_id or "c1",
        )
    assert not isinstance(result, authority.NeedChat)
    assert result.row is not None
    return result.row


def test_v4_first_qualified_pin_survives_same_file_boundary() -> None:
    entry_fact = _fact("entry", _file("/native/store/session.jsonl", inode=10))
    exit_fact = _fact("exit", _file("/native/store/session.jsonl", inode=10), order=2)
    builder = _fold(_begin())
    first = _accept(builder, entry_fact)
    authority.fold_row(builder, first)
    first_binding = builder.identity().native_bindings[authority.native_key_tuple(entry_fact.key)]
    second = _accept(builder, exit_fact)
    authority.fold_row(builder, second)
    binding = builder.identity().native_bindings[authority.native_key_tuple(entry_fact.key)]

    assert isinstance(first_binding.source, authority.Pinned)
    assert isinstance(binding.source, authority.Pinned)
    assert binding.source.observation == first_binding.source.observation
    assert binding.source.event_id == authority.boundary_digest_v4(entry_fact)
    assert binding.chat_id == "c1"
    assert builder.identity().key_to_chat[authority.native_key_tuple(entry_fact.key)] == "c1"


def test_v4_same_native_id_in_different_stores_gets_distinct_bindings() -> None:
    observation = _file("/native/store/session.jsonl", inode=10)
    first_fact = _fact("entry", observation)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, first_fact))

    second_begin = authority.BeginEventV4(
        run_id="other-run",
        attempt_id="other-attempt",
        transport_scope_id="other-transport",
        harness="pi",
        store="/native/other-store",
        operation="fresh",
        attempt_number=1,
    )
    second_fact = first_fact.model_copy(
        update={
            "run_id": "other-run",
            "attempt_id": "other-attempt",
            "key": _key(store="/native/other-store"),
            "evidence": first_fact.evidence.model_copy(
                update={"transport_scope_id": "other-transport"}
            ),
            "file": observation.model_copy(update={"path": "/native/other-store/session.jsonl"}),
        }
    )
    authority.fold_row(builder, second_begin)
    row = authority.plan_attempt_v4(
        builder.attempts,
        builder.latest,
        builder.identity(),
        second_fact,
    )
    assert isinstance(row, authority.NeedChat)
    accepted = authority.plan_attempt_v4(
        builder.attempts,
        builder.latest,
        builder.identity(),
        second_fact,
        assigned_chat="c2",
    )
    assert not isinstance(accepted, authority.NeedChat)
    assert accepted.row is not None
    authority.fold_row(builder, accepted.row)
    assert builder.identity().key_to_chat[authority.native_key_tuple(first_fact.key)] == "c1"
    assert builder.identity().key_to_chat[authority.native_key_tuple(second_fact.key)] == "c2"


def test_v4_resume_begin_requires_exact_current_recorded_source() -> None:
    fact = _fact("entry", _file("/native/store/session.jsonl", inode=10))
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, fact))
    binding = builder.identity().native_bindings[authority.native_key_tuple(fact.key)]
    assert isinstance(binding.source, authority.Pinned)
    assert binding.store_guard is not None
    source = authority.RecordedNativeSource(
        ref=authority.NativeSourceRef(
            chat_id=binding.chat_id,
            binding_event_id=binding.binding_event_id,
            locator_event_id=binding.source.event_id,
        ),
        key=fact.key,
        locator=binding.source.observation,
    )
    intent = authority.BeginIntentV4(
        run_id="resume-run",
        attempt_id="resume-attempt",
        transport_scope_id="resume-transport",
        harness="pi",
        store="/native/store",
        operation="resume",
        requested_source=source,
    )
    transition = authority.plan_attempt_v4(
        builder.attempts, builder.latest, builder.identity(), intent
    )
    assert not isinstance(transition, authority.NeedChat)
    assert isinstance(transition.row, authority.BeginEventV4)

    forged = intent.model_copy(
        update={
            "requested_source": source.model_copy(
                update={"ref": source.ref.model_copy(update={"chat_id": "c99"})}
            )
        }
    )
    with pytest.raises(ValueError, match="current unblocked recorded source"):
        authority.plan_attempt_v4(builder.attempts, builder.latest, builder.identity(), forged)


def test_v4_pending_can_pin_at_exit_and_exit_only_can_create_pin() -> None:
    pending = authority.PendingLocalFile(
        kind="local_file_pending",
        path="/native/store/later.jsonl",
        store_object={"device": 1, "inode": 10},
    )
    pending_entry = _fact("entry", pending)
    pin_exit = _fact("exit", _file("/native/store/actual.jsonl", inode=10), order=2)
    builder = _fold(_begin())
    row = _accept(builder, pending_entry)
    authority.fold_row(builder, row)
    row = _accept(builder, pin_exit)
    authority.fold_row(builder, row)
    binding = builder.identity().native_bindings[authority.native_key_tuple(pin_exit.key)]
    assert isinstance(binding.source, authority.Pinned)
    assert binding.source.event_id == authority.boundary_digest_v4(pin_exit)
    assert binding.store_guard is not None
    assert binding.store_guard.store_event_id == authority.boundary_digest_v4(pending_entry)

    exit_only = _fact(
        "exit", _file("/native/store/only.jsonl", inode=30), order=1, session_id="exit-only"
    )
    exit_builder = _fold(_begin("run", "attempt"))
    row = _accept(exit_builder, exit_only)
    authority.fold_row(exit_builder, row)
    exit_binding = exit_builder.identity().native_bindings[
        authority.native_key_tuple(exit_only.key)
    ]
    assert exit_binding.chat_id == "c1"
    assert isinstance(exit_binding.source, authority.Pinned)
    assert exit_builder.attempts[("run", "attempt")].entry is None


@pytest.mark.parametrize("first_reason", ["not_reported", "unsupported_locator"])
def test_v4_unobserved_retains_first_no_file_reason_and_provenance(first_reason: str) -> None:
    first = authority.NoFileObservation(kind="no_file_observation", reason=first_reason)
    later_reason = "unsupported_locator" if first_reason == "not_reported" else "not_reported"
    later = authority.NoFileObservation(kind="no_file_observation", reason=later_reason)
    entry = _fact("entry", first)
    exit_fact = _fact("exit", later, order=2)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, entry))
    authority.fold_row(builder, _accept(builder, exit_fact))
    binding = builder.identity().native_bindings[authority.native_key_tuple(entry.key)]

    assert isinstance(binding.source, authority.Unobserved)
    assert binding.source.observation == first
    assert binding.source.event_id == authority.boundary_digest_v4(entry)
    assert binding.store_guard is None


def test_v4_pending_retains_first_path_but_can_pin_a_later_file() -> None:
    pending_p = authority.PendingLocalFile(
        kind="local_file_pending",
        path="/native/store/p.jsonl",
        store_object={"device": 1, "inode": 10},
    )
    pending_q = pending_p.model_copy(update={"path": "/native/store/q.jsonl"})
    entry = _fact("entry", pending_p)
    later_pending = _fact("exit", pending_q, order=2)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, entry))
    authority.fold_row(builder, _accept(builder, later_pending))
    binding = builder.identity().native_bindings[authority.native_key_tuple(entry.key)]

    assert isinstance(binding.source, authority.Pending)
    assert binding.source.observation == pending_p
    assert binding.source.event_id == authority.boundary_digest_v4(entry)
    assert binding.store_guard is not None
    assert binding.store_guard.store_event_id == authority.boundary_digest_v4(entry)

    pin_q = _fact("exit", _file("/native/store/q.jsonl", inode=10), order=3)
    # The dormant v4 terminal policy does not accept a second exit; exercise the
    # source decision directly to cover the source-only pending-to-pin rule.
    decision = authority.plan_source(binding, pin_q, mode="assign")
    assert isinstance(decision, authority.SourceAdvanced)
    assert isinstance(decision.binding.source, authority.Pinned)
    assert decision.binding.source.observation.path == "/native/store/q.jsonl"
    assert decision.binding.source.event_id == authority.boundary_digest_v4(pin_q)
    assert decision.binding.store_guard == binding.store_guard


def test_v4_no_file_to_pending_to_pin_keeps_distinct_provenance() -> None:
    absent = authority.NoFileObservation(kind="no_file_observation", reason="not_reported")
    pending = authority.PendingLocalFile(
        kind="local_file_pending",
        path="/native/store/session.jsonl",
        store_object={"device": 1, "inode": 10},
    )
    entry = _fact("entry", absent)
    exit_fact = _fact("exit", pending, order=2)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, entry))
    authority.fold_row(builder, _accept(builder, exit_fact))
    binding = builder.identity().native_bindings[authority.native_key_tuple(entry.key)]
    assert isinstance(binding.source, authority.Pending)
    assert binding.source.event_id == authority.boundary_digest_v4(exit_fact)
    assert binding.store_guard is not None
    assert binding.store_guard.store_event_id == authority.boundary_digest_v4(exit_fact)

    pin = _fact("exit", _file(pending.path, inode=10), order=3)
    decision = authority.plan_source(binding, pin, mode="assign")
    assert isinstance(decision, authority.SourceAdvanced)
    assert isinstance(decision.binding.source, authority.Pinned)
    assert decision.binding.source.event_id == authority.boundary_digest_v4(pin)
    assert decision.binding.store_guard is not None
    assert decision.binding.store_guard.store_event_id == authority.boundary_digest_v4(exit_fact)


def test_v4_check_only_source_decision_does_not_acquire_guard_or_pin() -> None:
    absent = authority.NoFileObservation(kind="no_file_observation", reason="not_reported")
    entry = _fact("entry", absent)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, entry))
    binding = builder.identity().native_bindings[authority.native_key_tuple(entry.key)]
    later = _fact("exit", _file("/native/store/session.jsonl", inode=10), order=2)

    decision = authority.plan_source(binding, later, mode="check_only")
    assert isinstance(decision, authority.SourceUnchanged)
    assert decision.binding == binding
    assert decision.binding.store_guard is None
    assert isinstance(decision.binding.source, authority.Unobserved)


@pytest.mark.parametrize("conflict_kind", ["different_file", "store_replaced"])
def test_v4_conflict_is_absorbing_and_does_not_allocate_another_chat(conflict_kind: str) -> None:
    initial_obs: authority.NativeFileObservation
    if conflict_kind == "different_file":
        initial_obs = _file("/native/store/first.jsonl", inode=10)
        changed: authority.NativeFileObservation = _file(
            "/native/store/second.jsonl", inode=10, file_inode=19
        )
    else:
        initial_obs = authority.PendingLocalFile(
            kind="local_file_pending",
            path="/native/store/first.jsonl",
            store_object={"device": 1, "inode": 10},
        )
        changed = authority.PendingLocalFile(
            kind="local_file_pending",
            path="/native/store/second.jsonl",
            store_object={"device": 1, "inode": 20},
        )
    first_fact = _fact("entry", initial_obs)
    second_fact = _fact("exit", changed, order=2)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, first_fact))
    conflict = _accept(builder, second_fact)
    assert isinstance(conflict, authority.LocatorConflictEvent)
    authority.fold_row(builder, conflict)
    binding = builder.identity().native_bindings[authority.native_key_tuple(first_fact.key)]
    assert binding.conflict == conflict
    assert binding.chat_id == "c1"
    with pytest.raises(ValueError, match="blocked"):
        _accept(builder, _fact("exit", initial_obs, order=3))
    assert "c2" not in builder.identity().refs


def test_v4_conflict_target_is_replayed_against_prefix() -> None:
    original = _fact("entry", _file("/native/store/one.jsonl", inode=10))
    changed = _fact("exit", _file("/native/store/two.jsonl", inode=10, file_inode=22), order=2)
    builder = _fold(_begin())
    authority.fold_row(builder, _accept(builder, original))
    expected = _accept(builder, changed)
    assert isinstance(expected, authority.LocatorConflictEvent)
    for changes in (
        {"binding_event_id": "0" * 64},
        {"chat_id": "c99"},
        {"store_event_id": "0" * 64},
        {"locator_event_id": "0" * 64},
        {"reason": "store_replaced"},
    ):
        forged = expected.model_copy(update=changes)
        with pytest.raises(ValueError, match="Noncanonical"):
            authority.fold_row(builder, forged)
    authority.fold_row(builder, expected)
    with pytest.raises(ValueError, match="blocked"):
        authority.fold_row(builder, expected)


def test_v4_conflict_suppresses_legacy_key_and_effective_boundary_getters(tmp_path) -> None:
    original = _fact("entry", _file("/native/store/one.jsonl", inode=10))
    changed = _fact("exit", _file("/native/store/two.jsonl", inode=10, file_inode=22), order=2)
    builder = _fold(_begin())
    entry = _accept(builder, original)
    authority.fold_row(builder, entry)
    conflict = _accept(builder, changed)
    assert isinstance(conflict, authority.LocatorConflictEvent)
    rows = [_begin(), entry, conflict]
    journal = tmp_path / "sessions.jsonl"
    journal.write_text("".join(f"{row.model_dump_json()}\n" for row in rows))

    assert session_store.get_native_session_key(tmp_path, "c1") is None
    assert session_store.get_native_attempt_boundaries(tmp_path, "run", "attempt") == (
        None,
        None,
        False,
    )


@pytest.mark.parametrize("blocked_boundary", ["entry", "exit"])
def test_v4_boundary_getter_checks_entry_and_exit_bindings_independently(
    tmp_path, blocked_boundary: str
) -> None:
    entry_a = _fact("entry", _file("/native/store/a.jsonl", inode=10), session_id="a")
    builder = _fold(_begin())
    entry_row = _accept(builder, entry_a)
    authority.fold_row(builder, entry_row)
    rows: list[authority.V4AttemptEvent] = [_begin(), entry_row]

    if blocked_boundary == "entry":
        conflict_fact = _fact(
            "exit", _file("/native/store/a2.jsonl", inode=10, file_inode=22),
            order=2, session_id="a",
        )
        conflict = _accept(builder, conflict_fact)
        assert isinstance(conflict, authority.LocatorConflictEvent)
        authority.fold_row(builder, conflict)
        rows.append(conflict)
        exit_b = _fact("exit", _file("/native/store/b.jsonl", inode=30), order=3, session_id="b")
        exit_row = _accept(builder, exit_b, chat_id="c2")
        assert isinstance(exit_row, authority.BoundaryEventV4)
        authority.fold_row(builder, exit_row)
        rows.append(exit_row)
        expected = (None, "c2", False)
    else:
        exit_b = _fact("exit", _file("/native/store/b.jsonl", inode=30), order=2, session_id="b")
        exit_row = _accept(builder, exit_b, chat_id="c2")
        assert isinstance(exit_row, authority.BoundaryEventV4)
        authority.fold_row(builder, exit_row)
        rows.append(exit_row)
        conflict_fact = _fact(
            "exit", _file("/native/store/b2.jsonl", inode=30, file_inode=32),
            order=3, session_id="b",
        )
        conflict = _accept(builder, conflict_fact)
        assert isinstance(conflict, authority.LocatorConflictEvent)
        authority.fold_row(builder, conflict)
        rows.append(conflict)
        expected = ("c1", None, False)

    (tmp_path / "sessions.jsonl").write_text(
        "".join(f"{row.model_dump_json()}\n" for row in rows)
    )
    assert session_store.get_native_attempt_boundaries(tmp_path, "run", "attempt") == expected


def test_v4_codec_rejects_malformed_unknown_and_torn_authority_tail() -> None:
    with pytest.raises(ValueError):
        authority.decode_row({"event": "native_attempt", "v": 5, "action": "begin"})
    with pytest.raises(ValueError):
        authority.decode_row({"event": "native_attempt", "v": 4, "action": "future"})
    with pytest.raises(ValueError):
        authority.PendingLocalFile(
            kind="local_file_pending",
            path="/native/store/../outside",
            store_object={"device": 1, "inode": 10},
        )

    begin = _begin()
    fact = _fact(
        "entry", authority.NoFileObservation(kind="no_file_observation", reason="not_reported")
    )
    boundary = authority.BoundaryEventV4(
        run_id="run", attempt_id="attempt", fact=fact, chat_id="c1"
    )
    raw = (begin.model_dump_json() + "\n" + boundary.model_dump_json() + "\n{").encode()
    with pytest.raises(ValueError, match="native authority"):
        authority.read_journal(raw)


def test_v4_rows_round_trip_without_changing_v3_schema() -> None:
    event = _begin()
    assert authority.decode_row(json.loads(event.model_dump_json())) == event
    assert "file" not in authority.BoundaryFact.model_fields
