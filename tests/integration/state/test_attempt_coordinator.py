"""Owner topology and actual input gate, not production transport qualification."""

import asyncio
import json

import pytest

from meridian.lib.state import session_store as store
from meridian.lib.state.attempt_coordinator import (
    AttemptCoordinator,
    EntryWitness,
    ExitWitness,
    RefutationWitness,
)
from meridian.lib.state.session_authority import (
    BeginIntent,
    CreatedSelection,
    ResumeSelection,
    boundary_digest,
    read_journal,
)
from tests.support.attempt_owner import Observation, ScriptedOwner, key


def attempt(root, name="attempt", **context):
    owner = ScriptedOwner(
        BeginIntent(
            run_id="run",
            attempt_id=name,
            transport_scope_id=f"connection:{name}",
            harness="pi",
            store="/native/store",
            operation="fresh",
            **context,
        )
    )
    return owner, AttemptCoordinator(root, owner)


def entry(native_key=None):
    return EntryWitness(
        key=native_key or key("native"),
        order=1,
        correlation="primary-request",
        selection=CreatedSelection(creation_request="creation-request"),
    )


def terminal(native_key=None):
    return ExitWitness(
        key=native_key or key("native"),
        order=2,
        correlation="closed-dispatch-request",
        terminal_rule="fake-only:v1",
    )


def test_public_receipt_authority_path_is_removed():
    for name in (
        "begin_native_attempt",
        "accept_native_boundary",
        "refute_native_exit",
        "OwnedBoundaryReceipt",
        "BoundaryEvidence",
    ):
        assert not hasattr(store, name)


@pytest.mark.asyncio
async def test_owner_delivers_only_after_durable_ack_and_observes_outside_lock(tmp_path):
    from meridian.lib.platform.locking import try_lock_file

    class CheckingOwner(ScriptedOwner):
        async def observe_entry(self):
            with try_lock_file(
                store.RuntimePaths.from_root_dir(tmp_path).sessions_flock, reentrant=False
            ) as handle:
                assert handle is not None
            return await super().observe_entry()

        async def _deliver(self, task):
            snapshot = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot
            assert snapshot.attempts.states["run", "attempt"].entry is not None
            await super()._deliver(task)

    base, _ = attempt(tmp_path)
    owner = CheckingOwner(base.context)
    coordinator = AttemptCoordinator(tmp_path, owner)
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("context before begin")
    await coordinator.begin()
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("task before entry")
    owner.script.append(owner.observation(entry()))
    assert (await coordinator.commit_entry()).chat_id == "c1"
    await owner.deliver("task and injected context")
    assert owner.delivered == ["task and injected context"]
    assert owner.events == ["initialize", "observe_entry", "deliver"]
    with pytest.raises(ValueError, match="already"):
        AttemptCoordinator(tmp_path, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("requalification", ["changed", "unqualified"])
async def test_failed_entry_requalification_closes_previous_delivery_gate(
    tmp_path, requalification
):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(entry()))
    accepted = await coordinator.commit_entry()
    assert accepted.chat_id == "c1"

    changed = entry(key("native", "replacement"))
    owner.script.append(
        owner.observation(changed if requalification == "changed" else terminal())
    )
    with pytest.raises(ValueError):
        await coordinator.commit_entry()
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("must remain gated")
    assert owner.delivered == []
    assert coordinator._entry is None


@pytest.mark.asyncio
async def test_delivery_is_gated_while_entry_requalification_is_suspended(tmp_path):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(entry()))
    await coordinator.commit_entry()

    observing = asyncio.Event()
    release = asyncio.Event()
    original_observe = owner.observe_entry

    async def suspended_observe():
        observing.set()
        await release.wait()
        return await original_observe()

    owner.observe_entry = suspended_observe
    owner.script.append(owner.observation(entry()))
    requalification = asyncio.create_task(coordinator.commit_entry())
    await observing.wait()
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("must stay gated during qualification")
    release.set()
    assert (await requalification).chat_id == "c1"
    await owner.deliver("durably requalified")
    assert owner.delivered == ["durably requalified"]


@pytest.mark.asyncio
async def test_commit_exit_drains_uncertain_entry_but_returns_terminal_result(
    tmp_path, monkeypatch
):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(entry()))
    confirm = store._confirm_sessions_durability

    def fail_ack(path):
        raise OSError("entry append visible but acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_confirm_sessions_durability", fail_ack)
        for _ in range(2):
            with pytest.raises(OSError):
                await coordinator.commit_entry()
    pending_entry = coordinator._pending_entry
    assert pending_entry is not None

    owner.script.append(owner.observation(terminal()))
    with monkeypatch.context() as patch:
        patch.setattr(store, "_confirm_sessions_durability", fail_ack)
        with pytest.raises(OSError):
            await coordinator.commit_exit()
    # The close is single-flight even when pending entry durability still fails.
    await asyncio.sleep(0)
    assert owner.events == ["initialize", "observe_entry", "close_and_observe_exit"]
    result = await coordinator.commit_exit()
    state = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot
    assert result.chat_id == "c1"
    assert owner.events == ["initialize", "observe_entry", "close_and_observe_exit"]
    assert state.attempts.states["run", "attempt"].entry is not None
    assert state.attempts.states["run", "attempt"].exit is not None
    assert coordinator._pending_entry is None
    assert coordinator._pending_terminal is None
    assert store._confirm_sessions_durability is confirm


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["owner", "connection", "child", "store", "harness"])
async def test_copied_scope_wrong_owner_and_namespace_do_not_deliver(tmp_path, wrong):
    owner, coordinator = attempt(tmp_path)
    # Same serialized context is not the same connection or primary owner.
    other = ScriptedOwner(owner.context.model_copy())
    await coordinator.begin()
    witness = entry()
    source, connection, primary = owner, owner.connection, True
    if wrong == "owner":
        source = other
    elif wrong == "connection":
        connection = other.connection
    elif wrong == "child":
        primary = False
    else:
        witness = entry(
            witness.key.model_copy(update={wrong: "/different" if wrong == "store" else "claude"})
        )
    owner.script.append(Observation(source, connection, witness, primary))
    with pytest.raises(ValueError):
        await coordinator.commit_entry()
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("secret task/context")
    assert owner.delivered == []
    assert coordinator.boundaries().entry_chat_id is None


@pytest.mark.asyncio
async def test_wrong_resume_id_is_not_delivered(tmp_path):
    seed, first = attempt(tmp_path, "seed")
    await first.begin()
    seed.script.append(seed.observation(entry()))
    await first.commit_entry()
    owner = ScriptedOwner(
        BeginIntent(
            run_id="run",
            attempt_id="resume",
            transport_scope_id="resume-connection",
            harness="pi",
            store="/native/store",
            operation="resume",
            requested_source=key("native"),
        )
    )
    coordinator = AttemptCoordinator(tmp_path, owner)
    await coordinator.begin()
    owner.script.append(
        owner.observation(
            EntryWitness(
                key=key("wrong-id"),
                order=1,
                correlation="resume-request",
                selection=ResumeSelection(source=key("native")),
            )
        )
    )
    with pytest.raises(ValueError, match="pinned"):
        await coordinator.commit_entry()
    with pytest.raises(ValueError):
        await owner.deliver("do not deliver")
    assert owner.delivered == []


@pytest.mark.asyncio
async def test_missing_terminal_closes_input_without_inventing_exit(tmp_path):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(entry()))
    await coordinator.commit_entry()
    owner.script.append(owner.observation(entry()))  # last-seen selection, not terminal
    with pytest.raises(ValueError, match="missing terminal"):
        await coordinator.commit_exit()
    assert coordinator.boundaries().exit_chat_id is None
    with pytest.raises(ValueError):
        await owner.deliver("closed input")
    assert owner.delivered == []


@pytest.mark.asyncio
async def test_persistent_sync_failure_and_missing_file_never_deliver(tmp_path, monkeypatch):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    # Reserve before fault injection so failure is at the journal barrier.
    store.reserve_chat_id(tmp_path)
    original = store._confirm_sessions_durability

    def fail(path):
        raise OSError("persistent sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_confirm_sessions_durability", fail)
        for _ in range(2):
            owner.script.append(owner.observation(entry()))
            with pytest.raises(OSError):
                await coordinator.commit_entry()
            with pytest.raises(ValueError):
                await owner.deliver("not yet durable")
        assert owner.delivered == []
    owner.script.append(owner.observation(entry()))
    await coordinator.commit_entry()
    with monkeypatch.context() as patch:
        patch.setattr(store, "_confirm_sessions_durability", fail)
        with pytest.raises(OSError):
            await owner.deliver("visible but sync failed")
    assert store._confirm_sessions_durability is original
    (tmp_path / "sessions.jsonl").unlink()
    with pytest.raises(ValueError, match="recorded owner context"):
        await owner.deliver("journal disappeared")
    assert owner.delivered == []


@pytest.mark.asyncio
async def test_old_owner_consumer_survives_successor_and_retries_failed_refutation(
    tmp_path, monkeypatch
):
    owner, old = attempt(tmp_path, "old")
    await old.begin()
    owner.script.append(owner.observation(terminal()))
    old_chat = (await old.commit_exit()).chat_id
    # Consumer is already running before successor creation; it is not rebound.
    drain = asyncio.create_task(old.drain_refutations())
    await asyncio.sleep(0)
    successor, new = attempt(tmp_path, "new")
    await new.begin()
    successor.script.append(successor.observation(terminal(key("successor"))))
    new_chat = (await new.commit_exit()).chat_id
    snapshot = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot
    accepted = snapshot.attempts.states["run", "old"].exit
    assert accepted is not None
    witness = RefutationWitness(
        target_event_id=boundary_digest(accepted.fact),
        order=3,
        reason="finality_refuted",
        conflicting_key=key("native"),
        causal_reference="switch-after-apparent-terminal",
    )

    def fail(*args):
        raise OSError("refutation append unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_append_authority_event", fail)
        owner.refutation_queue.put_nowait(owner.observation(witness))
        with pytest.raises(OSError):
            await drain
        for _ in range(2):
            with pytest.raises(OSError):
                old.boundaries()  # Never replace an unrecorded contradiction with cached success.
    assert old.boundaries().exit_invalidated
    owner.refutation_queue.put_nowait(None)
    await old.drain_refutations()
    assert new.boundaries().exit_chat_id == new_chat
    assert store.get_native_session_key(tmp_path, str(old_chat)) == key("native")
    assert store.get_native_session_key(tmp_path, str(new_chat)) == key("successor")


@pytest.mark.asyncio
async def test_successor_closes_old_input_and_changed_recorded_context_refuses(tmp_path):
    owner, old = attempt(tmp_path)
    await old.begin()
    owner.script.append(owner.observation(entry()))
    await old.commit_entry()
    successor, new = attempt(tmp_path, "next")
    await new.begin()
    with pytest.raises(ValueError, match="superseded"):
        await owner.deliver("late task")
    # Context comes from the bound owner, not a witness with copied labels.
    path = tmp_path / "sessions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["transport_scope_id"] = "another-connection"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    successor.script.append(successor.observation(terminal()))
    with pytest.raises(ValueError, match="recorded owner context"):
        await new.commit_exit()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["owner", "connection", "child"])
async def test_foreign_refutation_cannot_revoke_owned_exit(tmp_path, wrong):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(terminal()))
    await coordinator.commit_exit()
    path = tmp_path / "sessions.jsonl"
    before = path.read_bytes()
    accepted = read_journal(before).snapshot.attempts.states["run", "attempt"].exit
    assert accepted is not None
    witness = RefutationWitness(
        target_event_id=boundary_digest(accepted.fact),
        order=3,
        reason="finality_refuted",
        causal_reference="copied-target-and-scope",
    )
    other = ScriptedOwner(owner.context.model_copy())
    owner.refutation_queue.put_nowait(
        Observation(
            other if wrong == "owner" else owner,
            other.connection if wrong == "connection" else owner.connection,
            witness,
            primary=wrong != "child",
        )
    )
    with pytest.raises(ValueError, match="not owned"):
        await coordinator.drain_refutations()
    assert path.read_bytes() == before
    assert coordinator.boundaries().exit_chat_id == "c1"


@pytest.mark.asyncio
async def test_unqualified_entry_never_delivers(tmp_path):
    owner, coordinator = attempt(tmp_path)
    await coordinator.begin()
    owner.script.append(owner.observation(terminal()))
    with pytest.raises(ValueError, match="no qualified entry"):
        await coordinator.commit_entry()
    with pytest.raises(ValueError, match="unresolved"):
        await owner.deliver("task/context")
    assert owner.delivered == []


@pytest.mark.asyncio
async def test_terminal_contradiction_survives_failed_append_and_retries_before_observation(
    tmp_path, monkeypatch
):
    owner, old = attempt(tmp_path, "old")
    await old.begin()
    owner.script.append(owner.observation(terminal()))
    old_chat = (await old.commit_exit()).chat_id
    successor, new = attempt(tmp_path, "new")
    await new.begin()
    successor.script.append(successor.observation(terminal(key("successor"))))
    new_chat = (await new.commit_exit()).chat_id
    path = tmp_path / "sessions.jsonl"
    before = path.read_bytes()
    owner.script.append(
        owner.observation(terminal(key("contradiction")).model_copy(update={"order": 3}))
    )

    def fail(*args, **kwargs):
        raise OSError("terminal contradiction append unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_append_session_row", fail)
        with pytest.raises(OSError):
            await old.commit_exit()
        for _ in range(2):
            with pytest.raises(OSError):
                old.boundaries()
            with pytest.raises(OSError):
                await old.commit_exit()  # Retry retained fact, not another observation.
        assert path.read_bytes() == before
    assert isinstance(await old.commit_exit(), store.UnresolvedBoundary)
    assert old.boundaries().exit_invalidated
    assert new.boundaries().exit_chat_id == new_chat
    assert store.get_native_session_key(tmp_path, str(old_chat)) == key("native")
    assert store.get_native_session_key(tmp_path, str(new_chat)) == key("successor")
    assert store.get_native_session_key(tmp_path, "c3") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["refutation_first", "terminal_first"])
@pytest.mark.parametrize("failure", ["append", "fsync"])
async def test_overlapping_terminal_and_refutation_are_retained_until_durable(
    tmp_path, monkeypatch, order, failure
):
    owner, old = attempt(tmp_path, "old")
    await old.begin()
    owner.script.append(owner.observation(terminal()))
    old_chat = (await old.commit_exit()).chat_id
    successor_owner, successor = attempt(tmp_path, "successor")
    await successor.begin()
    successor_owner.script.append(successor_owner.observation(terminal(key("successor"))))
    new_chat = (await successor.commit_exit()).chat_id

    snapshot = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot
    accepted = snapshot.attempts.states["run", "old"].exit
    assert accepted is not None
    witness = RefutationWitness(
        target_event_id=boundary_digest(accepted.fact),
        order=3,
        reason="finality_refuted",
        conflicting_key=key("native"),
        causal_reference="overlapping-finality-refutation",
    )
    append = store._append_authority_event
    durability = store._confirm_sessions_durability

    def fail_append(*args, **kwargs):
        raise OSError("persistent refutation append failure")

    def fail_fsync(path):
        raise OSError("persistent refutation fsync failure")

    def start_refutation_drain():
        owner.refutation_queue.put_nowait(owner.observation(witness))
        return asyncio.create_task(old.drain_refutations())

    def inject_failure():
        if failure == "append":
            monkeypatch.setattr(store, "_append_authority_event", fail_append)
        else:
            monkeypatch.setattr(store, "_confirm_sessions_durability", fail_fsync)

    if order == "refutation_first":
        owner.delay_exit = True
        owner.script.append(owner.observation(terminal()))
        exit_task = asyncio.create_task(old.commit_exit())
        await owner.exit_waiting.wait()
        inject_failure()
        drain = start_refutation_drain()
        # Wait until the owned observation has been consumed and its append fails.
        with pytest.raises(OSError):
            await drain
        owner.release_exit.set()
        with pytest.raises(OSError):
            await exit_task
    else:
        drain = asyncio.create_task(old.drain_refutations())
        await owner.refutation_waiting.wait()
        owner.script.append(owner.observation(terminal()))
        assert (await old.commit_exit()).chat_id == old_chat
        inject_failure()
        owner.refutation_queue.put_nowait(owner.observation(witness))
        with pytest.raises(OSError):
            await drain

    with pytest.raises(OSError):
        old.boundaries()
    during_failure = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot
    assert during_failure.attempts.states["run", "successor"].exit.chat_id == new_chat
    if failure == "append":
        monkeypatch.setattr(store, "_append_authority_event", append)
    else:
        monkeypatch.setattr(store, "_confirm_sessions_durability", durability)
    assert old.boundaries().exit_invalidated
    assert successor.boundaries().exit_chat_id == new_chat
    rows = read_journal((tmp_path / "sessions.jsonl").read_bytes()).snapshot.attempts.states
    assert rows["run", "old"].invalidation is not None
    assert rows["run", "successor"].invalidation is None
    assert rows["run", "successor"].exit is not None
