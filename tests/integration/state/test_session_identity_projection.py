"""All session claim paths share one normalized, prefix-checked identity view."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from meridian.lib.state import session_authority as authority
from meridian.lib.state import session_store as store


def historical(chat: str = "c1", **updates: object) -> dict:
    record = dict(
        chat_id=chat,
        record_mode="historical",
        kind="primary",
        harness="pi",
        harness_session_id=None,
        harness_session_ids=[],
        model="",
        agent="",
        agent_path="",
        skills=[],
        skill_paths=[],
        params=[],
        started_at="old",
        stopped_at="done",
        session_instance_id="historical",
    )
    record.update(updates)
    return {"event": "historical_import", "record": record}


def native(chat: str = "c1", attempt: str = "attempt", native_id: str = "native") -> list[dict]:
    key = store.NativeSessionKey(harness="pi", store="/native/store", native_session_id=native_id)
    receipt = store.OwnedBoundaryReceipt(
        run_id="run",
        attempt_id=attempt,
        boundary="entry",
        key=key,
        evidence=store.BoundaryEvidence(
            owner_attempt_id=attempt,
            transport_scope_id="transport",
            order=1,
            qualified=True,
            operation="fresh",
            before_delivery=True,
            fresh_creation_verified=True,
        ),
    )
    return [
        store.SessionAttemptEvent(
            action="begin",
            run_id="run",
            attempt_id=attempt,
            attempt_number=1,
            transport_scope_id="transport",
            operation="fresh",
        ).model_dump(mode="json"),
        store.SessionAttemptEvent(
            action="boundary",
            run_id="run",
            attempt_id=attempt,
            receipt=receipt,
            chat_id=chat,
        ).model_dump(mode="json"),
    ]


def journal(rows: list[dict]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([historical(" c100 ", forked_from_chat_id=" c190 ")], "c191"),
        ([historical("c¹"), historical("c\u0661"), historical("c01")], "c1"),
        ([{"event": "stop", "chat_id": " c50 ", "metadata": {"chat_id": "c999"}}], "c51"),
        (native(" c90 "), "c91"),
    ],
)
def test_normalized_schema_refs_not_arbitrary_payload_fields(tmp_path, rows, expected):
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(journal(rows))
    assert store.reserve_chat_id(tmp_path) == expected


@pytest.mark.parametrize(
    "rows",
    [
        [*native(), historical()],
        [historical(), *native()],
        [{"event": "stop", "chat_id": "c1"}, *native()],
        [historical("other", forked_from_chat_id=" c1 "), *native()],
        [historical("other", forked_from_chat_id=" c1 "), historical()],
        [historical(), {"event": "update", "chat_id": "c1", "session_instance_id": "new"}],
        [
            historical(),
            {
                "event": "start",
                "chat_id": "c1",
                "harness": "pi",
                "harness_session_id": "new",
                "model": "",
                "started_at": "later",
            },
        ],
        [historical(), historical()],
        [historical(), historical(model="different")],
    ],
)
def test_conflicting_prefix_refuses_every_authority_consumer_without_mutation(tmp_path, rows):
    path = tmp_path / "sessions.jsonl"
    raw = journal(rows) + b'{"torn":'
    path.write_bytes(raw)
    counter = store.RuntimePaths.from_root_dir(tmp_path).session_id_counter
    counter.write_text("900\n")
    for call in (
        lambda: store.reserve_chat_id(tmp_path),
        lambda: store.get_native_session_key(tmp_path, "c1"),
        lambda: store.get_native_attempt_boundaries(tmp_path, "run", "attempt"),
        lambda: store.begin_native_attempt(tmp_path, "other", "other", transport_scope_id="scope"),
    ):
        with pytest.raises(ValueError, match=r"row \d+.*(c1|historical)"):
            call()
        assert path.read_bytes() == raw
        assert counter.read_text() == "900\n"


@pytest.mark.parametrize("changed_chat, changed_key", [("c2", "native"), ("c1", "different")])
def test_both_native_uniqueness_directions_reject(tmp_path, changed_chat, changed_key):
    second = native(changed_chat, "second", changed_key)
    second[0]["attempt_number"] = 2
    path = tmp_path / "sessions.jsonl"
    raw = journal([*native(), *second])
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=r"(occupied|Duplicate native key)"):
        store.reserve_chat_id(tmp_path)
    assert path.read_bytes() == raw
    assert not store.RuntimePaths.from_root_dir(tmp_path).session_id_counter.exists()


def test_duplicate_pin_across_attempts_and_lifecycle_preserves_key(tmp_path):
    rows = native()
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(journal(rows))
    key = store.get_native_session_key(tmp_path, " c1 ")
    store.begin_native_attempt(tmp_path, "run", "second", transport_scope_id="transport")
    receipt = store.OwnedBoundaryReceipt.model_validate(native("c1", "second")[1]["receipt"])
    assert store.accept_native_boundary(tmp_path, receipt).chat_id == "c1"
    store.update_session_harness_id(tmp_path, "c1", "legacy-display-only")
    assert store.get_native_session_key(tmp_path, "c1") == key
    assert store.reserve_chat_id(tmp_path) == "c2"


@pytest.mark.parametrize(
    "prefix, proposed",
    [
        (native(), historical()),
        ([historical()], native()[1]),
        ([historical("other", forked_from_chat_id="c1")], historical()),
        ([{"event": "stop", "chat_id": "c1"}], native()[1]),
    ],
)
def test_proposed_identity_and_replay_use_identical_conflict_rules(tmp_path, prefix, proposed):
    raw = journal(prefix)
    view = authority.read_journal(raw).snapshot.identity
    event = authority.decode_row(proposed)
    with pytest.raises(ValueError) as live:
        authority.plan_identity(view, event)
    with pytest.raises(ValueError, match=str(live.value)):
        authority.read_journal(raw + journal([proposed]))
    # A rejected proposal must not even repair an otherwise eligible torn tail.
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(raw + b'{"torn":')
    before = path.read_bytes()
    with (
        pytest.raises(ValueError, match=str(live.value)),
        store._sessions_transaction(store.RuntimePaths.from_root_dir(tmp_path)) as transaction,
    ):
        store._append_proposed_row(path, transaction, event)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "row",
    [
        {"event": "future", "chat_id": "c1"},
        {"event": "stop", "v": 2, "chat_id": "c1"},
        {"event": "stop", "v": True, "chat_id": "c1"},
        {
            "event": "model_observation",
            "v": 9,
            "harness": "pi",
            "harness_session_id": "n",
            "observed_model_token": "model",
            "recorded_at": "now",
        },
        {"event": "historical_import", "v": 1, "record": historical()["record"]},
        {"event": "historical_import", "record": historical(stopped_at=None)["record"]},
    ],
)
@pytest.mark.parametrize("delimiter", [b"\n", b""])
def test_unsupported_or_invalid_complete_rows_are_never_repaired(tmp_path, row, delimiter):
    raw = json.dumps(row).encode() + delimiter
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.reserve_chat_id(tmp_path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("operation", ["reserve", "bind", "begin", "get", "boundaries"])
@pytest.mark.parametrize("delimiter", [True, False])
def test_normal_transaction_reads_decodes_and_folds_each_row_once(tmp_path, operation, delimiter):
    rows = native()
    rows.extend(
        [
            {"event": "stop", "chat_id": " c100 "},
            {
                "event": "model_observation",
                "harness": "pi",
                "harness_session_id": "n",
                "observed_model_token": "m",
                "recorded_at": "now",
            },
        ]
    )
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(journal(rows) if delimiter else journal(rows).rstrip(b"\n"))
    original_read = Path.read_bytes
    reads = []

    def read_bytes(self):
        if self == path:
            reads.append(self)
        return original_read(self)

    receipt = store.OwnedBoundaryReceipt.model_validate(rows[1]["receipt"])
    with (
        patch.object(Path, "read_bytes", read_bytes),
        patch.object(authority, "decode_row", wraps=authority.decode_row) as decode,
        patch.object(authority, "fold_row", wraps=authority.fold_row) as fold,
        patch.object(authority.json, "loads", wraps=authority.json.loads) as loads,
    ):
        if operation == "reserve":
            assert store.reserve_chat_id(tmp_path) == "c101"
        elif operation == "bind":
            # Exit changes target, allocating a new pin through the same snapshot.
            exit_receipt = receipt.model_copy(
                update={
                    "boundary": "exit",
                    "key": receipt.key.model_copy(update={"native_session_id": "other"}),
                    "evidence": receipt.evidence.model_copy(update={"order": 2, "terminal": True}),
                }
            )
            assert store.accept_native_boundary(tmp_path, exit_receipt).chat_id == "c101"
        elif operation == "begin":
            store.begin_native_attempt(tmp_path, "run", "new", transport_scope_id="new")
        elif operation == "get":
            assert store.get_native_session_key(tmp_path, "c1") == receipt.key
        else:
            assert (
                store.get_native_attempt_boundaries(tmp_path, "run", "attempt").entry_chat_id
                == "c1"
            )
        assert len(reads) == 1
        assert decode.call_count == fold.call_count == loads.call_count == len(rows)


def synthetic_archive(tmp_path):
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.state import spawn_store
    from meridian.lib.state.history import ingest_portable_history

    source = tmp_path / "source"
    spawn = str(
        spawn_store.start_spawn(
            source, chat_id="c1", model="test", agent="coder", harness="pi", prompt="synthetic"
        )
    )
    spawn_store.finalize_spawn(source, spawn, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        source,
        spawn,
        iter(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "synthetic"}]},
                },
            ]
        ),
    )
    archived = archive_history(source, destination=tmp_path / "zip", refs=(spawn,), apply=True)
    return Path(archived.archives[0]), archived.reclaimed


@pytest.mark.parametrize("interrupt_after", ["plan", "publication", "import"])
def test_legitimate_restore_preserves_native_pin_and_plan_retry(
    tmp_path, monkeypatch, interrupt_after
):
    from meridian.lib.state import retention_restore as restore

    archive, refs = synthetic_archive(tmp_path)
    root = tmp_path / "destination"
    root.mkdir()
    path = root / "sessions.jsonl"
    path.write_bytes(journal(native()))  # native-only high water; no counter
    original = restore.append_historical_session

    def interrupt(*args, **kwargs):
        if interrupt_after == "import":
            original(*args, **kwargs)
        raise RuntimeError("simulated interruption")

    with monkeypatch.context() as m:
        target = "_stage_record" if interrupt_after == "plan" else "append_historical_session"
        m.setattr(restore, target, interrupt)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            restore.restore_archive(root, archive, refs)
    plan_path = next((root / "history-archives/restores").glob("*.json"))
    plan = restore.RestorePlan.model_validate_json(plan_path.read_bytes())
    assert plan.chat_id == "c2"
    if interrupt_after != "plan":
        before = path.read_bytes()
        row_count = len(before.splitlines())
        with (
            patch.object(authority, "decode_row", wraps=authority.decode_row) as decode,
            patch.object(authority, "fold_row", wraps=authority.fold_row) as fold,
            patch.object(store, "read_journal", wraps=store.read_journal) as read,
        ):
            store.append_historical_session(root, plan.session, history_id=plan.session.history_id)
            assert read.call_count == 1
            assert decode.call_count == fold.call_count == row_count
        if interrupt_after == "import":
            assert path.read_bytes() == before  # live exact retry is NoOp
    result = restore.restore_archive(root, archive, refs)
    assert result == (plan.local_id,)
    assert restore.restore_archive(root, archive, refs) == result
    assert not plan_path.exists()
    assert store.reserve_chat_id(root) == "c3"
    assert store.get_session_record(root, "c1") is None
    assert store.get_session_record(root, "c2") == plan.session
    assert store.get_native_session_key(root, "c1").native_session_id == "native"
    assert store.get_native_session_key(root, "c2") is None
    assert len(path.read_bytes().splitlines()) == 3


def test_colliding_pending_plan_and_poisoned_import_noop_preserve_evidence(tmp_path, monkeypatch):
    from meridian.lib.state import retention_restore as restore

    archive, refs = synthetic_archive(tmp_path)
    root = tmp_path / "destination"

    def interrupt(*args, **kwargs):
        raise RuntimeError("before import")

    with monkeypatch.context() as m:
        m.setattr(restore, "append_historical_session", interrupt)
        with pytest.raises(RuntimeError, match="before import"):
            restore.restore_archive(root, archive, refs)
    plan_path = next((root / "history-archives/restores").glob("*.json"))
    plan_bytes = plan_path.read_bytes()
    plan = restore.RestorePlan.model_validate_json(plan_bytes)
    path = root / "sessions.jsonl"
    # Simulate a previously contaminated experiment. Do not remap its plan.
    path.write_bytes(journal(native(plan.chat_id)))
    before = path.read_bytes()
    counter = store.RuntimePaths.from_root_dir(root).session_id_counter
    counter_bytes = counter.read_bytes()
    aggregate = root / "spawns" / plan.local_id
    aggregate_bytes = {
        p.relative_to(aggregate): p.read_bytes() for p in aggregate.rglob("*") if p.is_file()
    }
    with pytest.raises(ValueError, match="occupied"):
        store.append_historical_session(root, plan.session, history_id=plan.session.history_id)
    assert path.read_bytes() == before
    assert counter.read_bytes() == counter_bytes
    assert plan_path.read_bytes() == plan_bytes
    assert aggregate_bytes == {
        p.relative_to(aggregate): p.read_bytes() for p in aggregate.rglob("*") if p.is_file()
    }
    # An exact historical record must not hide a later native claim on replay.
    path.write_bytes(
        journal(
            [
                store.SessionHistoricalEvent(record=plan.session).model_dump(mode="json"),
                *native(plan.chat_id),
            ]
        )
    )
    poisoned = path.read_bytes()
    with pytest.raises(ValueError, match="occupied"):
        store.append_historical_session(root, plan.session, history_id=plan.session.history_id)
    assert path.read_bytes() == poisoned
    assert counter.read_bytes() == counter_bytes
    assert plan_path.read_bytes() == plan_bytes


def test_every_schema_identity_effect_is_explicit(tmp_path):
    rows = [
        {
            "event": "start",
            "chat_id": " c10 ",
            "harness": "pi",
            "harness_session_id": None,
            "model": "",
            "started_at": "old",
            "forked_from_chat_id": " c70 ",
        },
        {"event": "update", "chat_id": " c20 "},
        {"event": "stop", "chat_id": " c30 "},
        {
            "event": "model_selection",
            "kind": "initial_seed",
            "harness": "pi",
            "harness_session_id": "n",
            "chat_id": " c40 ",
            "session_instance_id": "",
            "spawn_id": None,
            "startup_attempt_id": None,
            "recorded_at": "now",
            "selection": {"selection_source": "unknown", "provenance": {"chat_id": "c999"}},
        },
        historical(" c50 ", forked_from_chat_id=" c50 "),
        {
            "event": "model_observation",
            "harness": "pi",
            "harness_session_id": "c999",
            "observed_model_token": "m",
            "recorded_at": "now",
            "chat_id": "c999",
        },
        *native(" c60 "),
    ]
    raw = journal(rows)
    identity = authority.read_journal(raw).snapshot.identity
    assert set(identity.refs) == {"c10", "c20", "c30", "c40", "c50", "c60", "c70"}
    assert set(identity.chat_to_key) == {"c60"}
    assert isinstance(identity.refs["c70"], authority.ReferenceOnly)
    assert isinstance(identity.refs["c50"], authority.Historical)
    (tmp_path / "sessions.jsonl").write_bytes(raw)
    assert store.reserve_chat_id(tmp_path) == "c71"


def test_invalidation_retains_all_native_pins_and_high_water(tmp_path):
    path = tmp_path / "sessions.jsonl"
    rows = native()
    path.write_bytes(journal(rows))
    entry = store.OwnedBoundaryReceipt.model_validate(rows[1]["receipt"])
    exit_receipt = entry.model_copy(
        update={
            "boundary": "exit",
            "key": entry.key.model_copy(update={"native_session_id": "final"}),
            "evidence": entry.evidence.model_copy(update={"order": 2, "terminal": True}),
        }
    )
    assert store.accept_native_boundary(tmp_path, exit_receipt).chat_id == "c2"
    contradiction = exit_receipt.model_copy(
        update={
            "key": entry.key.model_copy(update={"native_session_id": "contradiction"}),
            "evidence": entry.evidence.model_copy(update={"order": 3, "terminal": True}),
        }
    )
    assert store.accept_native_boundary(tmp_path, contradiction).invalidated
    store.RuntimePaths.from_root_dir(tmp_path).session_id_counter.unlink()
    assert store.reserve_chat_id(tmp_path) == "c3"
    assert store.get_native_session_key(tmp_path, "c1") == entry.key
    assert store.get_native_session_key(tmp_path, "c2") == exit_receipt.key
    assert store.get_native_attempt_boundaries(tmp_path, "run", "attempt").exit_invalidated
