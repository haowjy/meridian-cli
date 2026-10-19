"""Observed identities inherit context; verified exits additionally require a transcript."""

import json
from functools import partial
from pathlib import Path

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.spawn.model import RunBoundaryOutcome, SpawnRecord


def test_observed_chat_refuses_unsupported_native_scope_without_duplicate(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    native_store = str(tmp_path / "native")
    source = session_store.start_session(
        runtime,
        harness="claude",
        harness_session_id="entry",
        model="test",
        native_store=native_store,
    ).chat_id
    session_store.stop_session(runtime, source)
    spawn_store.start_spawn(
        runtime,
        spawn_id="p1",
        chat_id=source,
        harness="claude",
        kind="primary",
        harness_session_id="entry",
        model="test",
        agent="",
        prompt="",
    )
    sessions_path = runtime / "sessions.jsonl"
    with sessions_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "v": 2,
                    "event": "start",
                    "chat_id": "c-future",
                    "kind": "primary",
                    "harness": "claude",
                    "harness_session_id": "future-native",
                    "native_store": native_store,
                    "model": "test",
                    "session_instance_id": "future-generation",
                    "started_at": "2026-10-19T00:00:00Z",
                }
            )
            + "\n"
        )
    original = sessions_path.read_bytes()

    with pytest.raises(ValueError, match="unsupported session"):
        session_store.get_or_create_observed_chat(
            runtime,
            source,
            "claude",
            native_store,
            "future-native",
            spawn_id="p1",
        )

    assert sessions_path.read_bytes() == original
    row = spawn_store.get_spawn(runtime, "p1")
    assert row is not None
    assert row.current_chat_id is None


def test_verified_exit_publishes_only_with_native_proof_on_creation_and_reuse(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    store = str(tmp_path / "native")
    entry = session_store.start_session(
        runtime, harness="claude", harness_session_id="entry", model="", native_store=store,
    ).chat_id
    session_store.stop_session(runtime, entry)
    spawn_store.start_spawn(
        runtime, spawn_id="p1", chat_id=entry, harness="claude", kind="primary",
        harness_session_id="entry", model="", agent="", prompt="",
    )
    transcript = tmp_path / "native/exit.jsonl"
    publish = partial(
        session_store.get_or_create_observed_chat, runtime, entry, "claude", store,
        spawn_id="p1", native_exists=transcript.is_file,
    )

    def current() -> str | None:
        row = spawn_store.get_spawn(runtime, "p1")
        return row.current_chat_id if row is not None else None

    assert publish("exit") is None
    assert len(session_store.list_all_session_records(runtime)) == 1 and current() is None
    transcript.parent.mkdir()
    transcript.write_text('{"sessionId":"exit"}\n')
    exit_chat = publish("exit")
    assert exit_chat is not None and exit_chat != entry and current() == exit_chat
    record = session_store.chat_record(runtime, exit_chat)
    assert record is not None and record.stopped_at is not None
    assert publish("exit") == exit_chat
    transcript.unlink()
    assert publish("exit") is None  # Existing *other* chat must not bypass proof.
    assert publish("entry") is None  # Nor may the entry chat itself.
    assert current() == exit_chat
    assert len(session_store.list_all_session_records(runtime)) == 2


def test_spawn_ref_settings_follow_selected_chat_whether_or_not_observed(tmp_path: Path):
    runtime = tmp_path / "runtime"
    store = str(tmp_path / "native")
    chat = session_store.start_session(
        runtime, harness="claude", harness_session_id="entry", model="",
        native_store=store, kind="primary",
    ).chat_id
    session_store.update_session_work_id(runtime, chat, "original-work")
    session_store.stop_session(runtime, chat)
    spawn_store.start_spawn(
        runtime, spawn_id="p1", chat_id=chat, harness="claude", kind="primary",
        harness_session_id="entry", work_id="original-work", model="", agent="", prompt="",
    )
    session_store.update_session_work_id(runtime, chat, "selected-chat-work")

    def work(ref: str) -> str | None:
        return resolve_session_reference(tmp_path, ref, runtime_root=runtime).source_work_id

    assert work("p1") == work(chat) == "selected-chat-work"
    assert session_store.get_or_create_observed_chat(
        runtime, chat, "claude", store, "entry", spawn_id="p1",
    ) == chat  # Observing the same conversation changes nothing.
    assert work("p1") == work(chat) == "selected-chat-work"
    spawn_store.update_spawn(runtime, "p1", current_chat_id="c999")
    with pytest.raises(NativeSessionUnavailable):  # Never fall back to the older entry.
        resolve_session_reference(tmp_path, "p1", runtime_root=runtime)


def test_observed_chat_inherits_metadata_without_transcript_or_live_lease(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    source = session_store.start_session(
        runtime,
        harness="claude",
        harness_session_id="entry",
        model="model-a",
        params=("--flag",),
        agent="builder",
        agent_path="/agents/builder.md",
        skills=("one",),
        skill_paths=("/skills/one",),
        control_root=str(tmp_path / "control"),
        task_cwd=str(tmp_path / "task"),
        execution_cwd=str(tmp_path / "exec"),
        native_store=str(tmp_path / "native"),
        claude_config_dir=str(tmp_path / "config"),
        kind="primary",
    ).chat_id
    session_store.update_session_work_id(runtime, source, "work-a")
    snapshot = LaunchPolicySnapshot(model="model-a", harness="claude", agent="builder")
    generation = session_store.chat_record(runtime, source)
    assert generation is not None  # Final binding gives the entry its own policy.
    session_store.update_session_launch_policy(
        runtime, source, generation.session_instance_id, snapshot,
    )
    session_store.stop_session(runtime, source)
    spawn_store.start_spawn(
        runtime, spawn_id="p1", chat_id=source, harness="claude", work_id="work-a",
        model="model-a", agent="builder", prompt="task", kind="primary",
        harness_session_id="entry", launch_policy_snapshot=snapshot,
    )
    original = session_store.chat_record(runtime, source)
    assert original is not None

    observed = session_store.get_or_create_observed_chat(
        runtime,
        source,
        "claude",
        str(tmp_path / "native"),
        "observed",
        spawn_id="p1",
    )
    record = session_store.chat_record(runtime, observed)
    assert record is not None
    assert record.chat_id != source
    assert record.harness_session_id == "observed"
    assert record.model == "model-a"
    assert record.agent == "builder"
    assert record.agent_path == "/agents/builder.md"
    assert record.skills == ("one",)
    assert record.skill_paths == ("/skills/one",)
    assert record.params == ("--flag",)
    assert record.active_work_id == "work-a"
    assert record.spawn_id is None  # An observation is not another execution.
    assert record.launch_policy_snapshot == snapshot
    assert (runtime / "sessions.jsonl").stat().st_mode & 0o777 == 0o600
    assert record.stopped_at is not None
    assert record.session_instance_id != original.session_instance_id
    assert record.history_id is not None and record.history_id != original.history_id
    assert record.forked_from_chat_id is None
    assert record.control_root == original.control_root
    assert record.task_cwd == original.task_cwd
    assert record.execution_cwd == original.execution_cwd
    assert record.claude_config_dir == original.claude_config_dir
    assert session_store.list_active_sessions(runtime) == []
    assert session_store.chat_record(runtime, source) == original
    row = spawn_store.get_spawn(runtime, "p1")
    assert row is not None and row.current_chat_id == observed
    assert row is not None and row.chat_id == source and row.harness_session_id == "entry"
    for ref in ("p1", observed):
        selected = resolve_session_reference(tmp_path, ref, runtime_root=runtime)
        assert selected.source_chat_id == observed
        assert selected.harness_session_id == "observed"
        assert selected.source_work_id == "work-a"
        assert selected.source_launch_policy_snapshot == snapshot
    assert resolve_session_reference(
        tmp_path, source, runtime_root=runtime,
    ).harness_session_id == "entry"

    # Switching the current chat's work must govern the next /new; reuse doesn't reset it.
    session_store.update_session_work_id(runtime, observed, "work-b")
    assert session_store.get_or_create_observed_chat(
        runtime, source, "claude", str(tmp_path / "native"), "observed", spawn_id="p1"
    ) == observed
    assert session_store.get_session_active_work_id(runtime, observed) == "work-b"
    following = session_store.get_or_create_observed_chat(
        runtime, observed, "claude", str(tmp_path / "native"), "following", spawn_id="p1"
    )
    assert session_store.get_session_active_work_id(runtime, following) == "work-b"
    session_store.update_session_work_id(runtime, following, "")
    assert resolve_session_reference(
        tmp_path, "p1", runtime_root=runtime,
    ).source_work_id is None  # Explicit detachment must not fall back to old spawn work.

    # Resume an existing conversation, then /new: inherit its policy, not p1's.
    other_policy = LaunchPolicySnapshot(
        model="model-b", harness="claude", agent="reviewer", env={"OBSERVED_SETTING": "kept"},
    )
    other_id = session_store.reserve_chat_id(runtime)
    spawn_store.start_spawn(
        runtime, spawn_id="p2", chat_id=other_id, harness="claude",
        model="model-b", agent="reviewer", prompt="other", kind="primary",
        launch_policy_snapshot=other_policy,
    )
    other = session_store.start_session(
        runtime, chat_id=other_id, spawn_id="p2", harness="claude",
        harness_session_id="existing", native_store=str(tmp_path / "other-native"),
        model="model-b", agent="reviewer", kind="primary",
    ).chat_id
    session_store.update_session_work_id(runtime, other, "other-work")
    session_store.stop_session(runtime, other)
    before = session_store.chat_record(runtime, other)
    assert session_store.get_or_create_observed_chat(
        runtime, following, "claude", str(tmp_path / "other-native"), "existing", spawn_id="p1",
    ) == other
    assert session_store.chat_record(runtime, other) == before
    newest = session_store.get_or_create_observed_chat(
        runtime, source, "claude", str(tmp_path / "other-native"), "newest", spawn_id="p1",
    )  # The locked current pointer wins over this stale source argument.
    selected = resolve_session_reference(tmp_path, newest, runtime_root=runtime)
    assert selected.source_launch_policy_snapshot == other_policy
    assert selected.source_work_id == "other-work"
    assert resolve_session_reference(
        tmp_path, "p1", runtime_root=runtime,
    ).source_launch_policy_snapshot == other_policy
    assert session_store.chat_record(runtime, source) == original
    assert session_identity.get_session_record_for_spawn(runtime, "p1") == original


def test_observed_chat_is_protected_by_its_live_run_not_a_copied_lease(tmp_path: Path):
    runtime = tmp_path / "runtime"
    entry = session_store.reserve_chat_id(runtime)
    spawn_store.start_spawn(
        runtime, spawn_id="p1", chat_id=entry, harness="claude", kind="primary",
        model="", agent="", prompt="",
    )
    session_store.start_session(
        runtime, chat_id=entry, spawn_id="p1", harness="claude", model="",
        harness_session_id="entry", native_store=str(tmp_path / "native"),
    )
    try:
        current = session_store.get_or_create_observed_chat(
            runtime, entry, "claude", str(tmp_path / "native"), "current", spawn_id="p1",
        )
        assert not session_store.is_session_lease_owner_alive(runtime, current)
        assert session_identity.live_observer_for_chat(runtime, current) == "p1"
        with pytest.raises(ValueError, match="in use by live run p1"):
            session_store.start_session(
                runtime, chat_id=current, harness="claude", model="",
                harness_session_id="current", native_store=str(tmp_path / "native"),
            )
    finally:
        session_store.stop_session(runtime, entry)
    assert session_identity.live_observer_for_chat(runtime, current) is None
    session_store.start_session(
        runtime, chat_id=current, harness="claude", model="",
        harness_session_id="current", native_store=str(tmp_path / "native"),
    )
    session_store.stop_session(runtime, current)


def test_spawn_current_chat_precedes_boundary_but_not_mismatch():
    row = SpawnRecord(
        id="p1", status="running", chat_id="c-old", current_chat_id="c-new"
    )
    assert row.continue_chat_id == "c-new"
    terminal = row.model_copy(update={"status": "succeeded"})
    assert terminal.continue_chat_id == "c-new"
    assert terminal.model_copy(update={"record_mode": "historical"}).continue_chat_id is None
    mismatch = row.model_copy(update={
        "run_boundary": RunBoundaryOutcome(status="mismatch"),
    })
    assert mismatch.continue_chat_id == "c-old"
