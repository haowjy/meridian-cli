"""Attempt transitions must have identical live and strict-replay policy.

Scripted owners exercise consistency only, not production fork ancestry.
"""

import json
from pathlib import Path

import pytest

from meridian.lib.state import session_authority as authority
from meridian.lib.state import session_store as store
from tests.support.attempt_owner import begin, key, observe, receipt, refute


def test_superseded_exit_can_be_refuted_without_assigning_old_boundaries(tmp_path: Path):
    begin(tmp_path, "run", "old")
    accepted = receipt("run", "old", "exit", key("/native/old"), order=2)
    pinned = observe(tmp_path, accepted).chat_id
    begin(tmp_path, "run", "new")
    successor = observe(tmp_path, receipt("run", "new", "exit", key("/native/new")))
    assert observe(tmp_path, accepted).chat_id == pinned
    contradiction = receipt("run", "old", "exit", key("/native/conflict"), order=3)
    assert observe(tmp_path, contradiction).invalidated
    assert store.get_native_attempt_boundaries(tmp_path, "run", "old").exit_invalidated
    assert (
        store.get_native_attempt_boundaries(tmp_path, "run", "new").exit_chat_id
        == successor.chat_id
    )
    assert store.get_native_session_key(tmp_path, str(pinned)) == accepted.key
    before = (tmp_path / "sessions.jsonl").read_bytes()
    for observation in (accepted, contradiction):
        assert observe(tmp_path, observation).invalidated
    with pytest.raises(ValueError):
        observe(tmp_path, receipt("run", "old", "entry", accepted.key))
    assert (tmp_path / "sessions.jsonl").read_bytes() == before


def test_equal_order_different_key_refutes_exit(tmp_path: Path):
    begin(tmp_path, "run", "attempt")
    observe(tmp_path, receipt("run", "attempt", "exit", key("/native/one")))
    assert observe(tmp_path, receipt("run", "attempt", "exit", key("/native/two"))).invalidated
    assert store.get_native_session_key(tmp_path, "c1") == key("/native/one")
    assert store.get_native_session_key(tmp_path, "c2") is None


def test_same_key_finality_refutation_is_bounded_and_absorbing(tmp_path: Path):
    begin(tmp_path, "run", "attempt")
    accepted = receipt("run", "attempt", "exit", key("/native/one"))
    observe(tmp_path, accepted)
    refutation = authority.Refutation(
        run_id="run",
        attempt_id="attempt",
        transport_scope_id="transport:attempt",
        target_event_id=authority.boundary_digest(accepted),
        order=2,
        reason="finality_refuted",
        conflicting_key=accepted.key,
        causal_reference="input-gate-reopened-after-terminal",
    )
    assert refute(tmp_path, refutation).invalidated
    before = (tmp_path / "sessions.jsonl").read_bytes()
    assert refute(tmp_path, refutation).invalidated
    assert observe(tmp_path, accepted).invalidated
    assert (tmp_path / "sessions.jsonl").read_bytes() == before
    assert store.get_native_session_key(tmp_path, "c1") == accepted.key


def test_fork_begin_retains_source_and_requires_distinct_evidenced_target(tmp_path: Path):
    source = key("/native/source")
    begin(tmp_path, "source", "source")
    observe(tmp_path, receipt("source", "source", "entry", source))
    begin(tmp_path, "fork", "fork", operation="fork", requested_source=source)
    with pytest.raises(ValueError):
        observe(
            tmp_path, receipt("fork", "fork", "entry", source, operation="fork", source_key=source)
        )
    target = receipt(
        "fork", "fork", "entry", key("/native/target"), operation="fork", source_key=source
    )
    assert observe(tmp_path, target).chat_id == "c2"
    assert store.get_native_session_key(tmp_path, "c1") == source


@pytest.mark.parametrize("mutation", ["scope", "source", "order", "assignment"])
def test_replay_refuses_mutated_valid_rows_without_changing_bytes(tmp_path: Path, mutation: str):
    source = key("/native/source")
    begin(tmp_path, "seed", "seed")
    observe(tmp_path, receipt("seed", "seed", "entry", source))
    begin(tmp_path, "run", "attempt", operation="resume", requested_source=source)
    observe(
        tmp_path, receipt("run", "attempt", "entry", source, operation="resume", source_key=source)
    )
    observe(
        tmp_path,
        receipt("run", "attempt", "exit", source, order=2, operation="resume", source_key=source),
    )
    path = tmp_path / "sessions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "scope":
        rows[-1]["fact"]["evidence"]["transport_scope_id"] = "child"
    elif mutation == "source":
        rows[-2]["fact"]["evidence"]["selection"]["source"]["native_session_id"] = "wrong"
    elif mutation == "order":
        rows[-1]["fact"]["evidence"]["order"] = 0
    else:
        rows[-1]["chat_id"] = "c99"
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(raw)
    counter = (tmp_path / "session-id-counter").read_bytes()
    for call in (
        lambda: store.get_native_session_key(tmp_path, "c1"),
        lambda: store.get_native_attempt_boundaries(tmp_path, "run", "attempt"),
        lambda: store.reserve_chat_id(tmp_path),
    ):
        with pytest.raises(ValueError):
            call()
        assert path.read_bytes() == raw
        assert (tmp_path / "session-id-counter").read_bytes() == counter


@pytest.mark.parametrize(
    "mutation", ["owner", "operation", "source", "number", "superseded", "retroactive", "duplicate"]
)
def test_strict_replay_checks_begin_context_and_assignment_phase(tmp_path: Path, mutation: str):
    begin(tmp_path, "run", "first")
    observe(tmp_path, receipt("run", "first", "entry", key("/native/one")))
    observe(tmp_path, receipt("run", "first", "exit", key("/native/one"), order=2))
    begin(tmp_path, "run", "second")
    path = tmp_path / "sessions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "owner":
        rows[0]["transport_scope_id"] = "other-owner"
    elif mutation == "operation":
        rows[0]["operation"] = "resume"
    elif mutation == "source":
        rows[0]["requested_source"] = key("/native/one").model_dump()
    elif mutation == "number":
        rows[-1]["attempt_number"] = 9
    elif mutation == "superseded":
        rows = [rows[0], rows[3], rows[1], rows[2]]
    elif mutation == "retroactive":
        rows = [rows[0], rows[2], rows[1], rows[3]]
    else:
        rows.append(rows[2])
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.get_native_session_key(tmp_path, "c1")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("operation", ["fresh", "resume", "fork"])
def test_live_context_retries_exit_without_entry_and_no_retroactive_entry(
    tmp_path: Path, operation: str
):
    source = key("/native/source")
    begin(tmp_path, "seed", "seed")
    observe(tmp_path, receipt("seed", "seed", "entry", source))
    requested = None if operation == "fresh" else source
    begin(tmp_path, "run", "attempt", operation=operation, requested_source=requested)
    path = tmp_path / "sessions.jsonl"
    before = path.read_bytes()
    begin(tmp_path, "run", "attempt", operation=operation, requested_source=requested)
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        begin(
            tmp_path,
            "run",
            "attempt",
            operation=operation,
            requested_source=requested,
            transport_scope_id="changed",
        )
    target = key("/native/target")
    # Exit is independent of resume equality and fresh/fork creation assertions.
    observation = receipt(
        "run", "attempt", "exit", target, operation=operation, source_key=requested
    )
    accepted = observe(tmp_path, observation)
    assert accepted.chat_id == "c2"
    assert store.get_native_attempt_boundaries(tmp_path, "run", "attempt").entry_chat_id is None
    before = path.read_bytes()
    with pytest.raises(ValueError):
        observe(
            tmp_path,
            receipt(
                "run",
                "attempt",
                "entry",
                source if operation == "resume" else target,
                operation=operation,
                source_key=requested,
            ),
        )
    confirmation = observation.model_copy(
        update={"evidence": observation.evidence.model_copy(update={"order": 5})}
    )
    assert observe(tmp_path, confirmation) == accepted
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    ["child", "target", "stale", "same-key-identity", "equal-identity", "later-same-boundary"],
)
def test_refutation_rejects_wrong_target_owner_and_causality_live_and_replay(
    tmp_path: Path, bad: str
):
    begin(tmp_path, "run", "attempt")
    accepted = receipt("run", "attempt", "exit", key("/native/one"), order=2)
    observe(tmp_path, accepted)
    fields = dict(
        run_id="run",
        attempt_id="attempt",
        transport_scope_id="transport:attempt",
        target_event_id=authority.boundary_digest(accepted),
        order=3,
        reason="identity_conflict",
        conflicting_key=key("/native/two"),
        causal_reference="observed-conflict",
    )
    if bad == "child":
        fields["transport_scope_id"] = "child"
    elif bad == "target":
        fields["target_event_id"] = "f" * 64
    elif bad == "stale":
        fields.update(reason="finality_refuted", order=1)
    elif bad == "same-key-identity":
        fields["conflicting_key"] = accepted.key
    elif bad == "equal-identity":
        fields["order"] = 2
    else:
        fields["reason"] = "same_boundary_conflict"
    refutation = authority.Refutation(**fields)
    path = tmp_path / "sessions.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        refute(tmp_path, refutation)
    assert path.read_bytes() == before
    raw = before + refutation.model_dump_json().encode() + b"\n"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.get_native_attempt_boundaries(tmp_path, "run", "attempt")
    assert path.read_bytes() == raw


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_facts_are_preserved_and_never_promoted(tmp_path: Path, version: int):
    # Frozen caller-assertion schema, never silently promoted to owner observations.
    raw = (
        b'{"v":1,"event":"native_attempt","action":"begin","run_id":"r","attempt_id":"a",'
        b'"attempt_number":1,"transport_scope_id":"scope","operation":"fresh"}\n'
    )
    raw = raw.replace(b'"v":1', f'"v":{version}'.encode())
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(raw)
    for call in (
        lambda: store.get_native_session_key(tmp_path, "c1"),
        lambda: store.reserve_chat_id(tmp_path),
        lambda: begin(tmp_path, "new", "new"),
    ):
        with pytest.raises(ValueError, match="reconciliation"):
            call()
        assert path.read_bytes() == raw


def test_fork_missing_ancestry_and_wrong_resume_source_are_not_accepted(tmp_path: Path):
    source = key("/native/source")
    begin(tmp_path, "seed", "seed")
    observe(tmp_path, receipt("seed", "seed", "entry", source))
    for operation in ("resume", "fork"):
        with pytest.raises(ValueError):
            begin(tmp_path, "missing", operation, operation=operation)
        begin(tmp_path, operation, operation, operation=operation, requested_source=source)
        observation = receipt(
            operation,
            operation,
            "entry",
            key("/native/target"),
            operation=operation,
            source_key=source,
        )
        if operation == "fork":
            observation = observation.model_copy(
                update={"evidence": observation.evidence.model_copy(update={"selection": None})}
            )
        with pytest.raises(ValueError):
            observe(tmp_path, observation)
    assert store.get_native_session_key(tmp_path, "c2") is None


@pytest.mark.parametrize("operation", ["fresh", "fork"])
def test_entry_requires_creation_or_ancestry_evidence_live_and_replay(
    tmp_path: Path, operation: str
):
    source = key("/native/source")
    begin(tmp_path, "seed", "seed")
    observe(tmp_path, receipt("seed", "seed", "entry", source))
    requested = source if operation == "fork" else None
    begin(tmp_path, "run", "attempt", operation=operation, requested_source=requested)
    accepted = receipt(
        "run", "attempt", "entry", key("/native/target"), operation=operation, source_key=requested
    )
    rejected = accepted.model_copy(
        update={"evidence": accepted.evidence.model_copy(update={"selection": None})}
    )
    path = tmp_path / "sessions.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        observe(tmp_path, rejected)
    assert path.read_bytes() == before
    observe(tmp_path, accepted)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["fact"]["evidence"]["selection"] = None
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.get_native_session_key(tmp_path, "c2")
    assert path.read_bytes() == raw
