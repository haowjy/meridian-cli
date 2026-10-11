"""Observed identities inherit context; verified exits additionally require a transcript."""

from functools import partial
from pathlib import Path

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.model import RunBoundaryOutcome, SpawnRecord


def test_exit_chat_requires_persistence_on_creation_and_reuse(tmp_path: Path):
    runtime = tmp_path / ".meridian"
    store = str(tmp_path / "native")
    entry = session_store.start_session(
        runtime, harness="claude", harness_session_id="entry", model="", native_store=store,
    )
    session_store.stop_session(runtime, entry)
    transcript = tmp_path / "native/exit.jsonl"
    allocate = partial(
        session_store.get_or_create_exit_chat, runtime, entry, "claude", store, "exit",
        native_exists=transcript.is_file,
    )
    assert allocate() is None
    assert len(session_store.list_all_session_records(runtime)) == 1
    transcript.parent.mkdir()
    transcript.write_text('{"sessionId":"exit"}\n')
    exit_chat = allocate()
    assert exit_chat is not None and exit_chat != entry
    record = session_store.get_session_record(runtime, exit_chat)
    assert record is not None and record.stopped_at is not None
    assert allocate() == exit_chat
    assert session_store.get_or_create_exit_chat(
        runtime, exit_chat, "claude", store, "exit", native_exists=transcript.is_file,
    ) == exit_chat
    transcript.unlink()
    assert allocate() is None  # Existing *other* chat must not bypass proof.
    assert session_store.get_or_create_exit_chat(
        runtime, exit_chat, "claude", store, "exit", native_exists=transcript.is_file,
    ) is None  # Nor may the entry chat itself.
    assert len(session_store.list_all_session_records(runtime)) == 2


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
    )
    session_store.update_session_work_id(runtime, source, "work-a")
    session_store.stop_session(runtime, source)
    snapshot = LaunchPolicySnapshot(model="model-a", harness="claude", agent="builder")
    spawn_store.start_spawn(
        runtime, spawn_id="p1", chat_id=source, harness="claude", work_id="work-a",
        model="model-a", agent="builder", prompt="task", kind="primary",
        harness_session_id="entry", launch_policy_snapshot=snapshot,
    )
    original = session_store.get_session_record(runtime, source)
    assert original is not None

    observed = session_store.get_or_create_observed_chat(
        runtime,
        source,
        "claude",
        str(tmp_path / "native"),
        "observed",
        spawn_id="p1",
    )
    record = session_store.get_session_record(runtime, observed)
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
    assert record.spawn_id == "p1"
    assert record.stopped_at is not None
    assert record.session_instance_id != original.session_instance_id
    assert record.history_id is not None and record.history_id != original.history_id
    assert record.forked_from_chat_id is None
    assert record.control_root == original.control_root
    assert record.task_cwd == original.task_cwd
    assert record.execution_cwd == original.execution_cwd
    assert record.claude_config_dir == original.claude_config_dir
    assert session_store.list_active_sessions(runtime) == []
    assert session_store.get_session_record(runtime, source) == original
    spawn_store.update_spawn(runtime, "p1", current_chat_id=observed)
    row = spawn_store.get_spawn(runtime, "p1")
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
    session_store.update_session_work_id(runtime, observed, "")
    assert resolve_session_reference(
        tmp_path, "p1", runtime_root=runtime,
    ).source_work_id is None  # Explicit detachment must not fall back to old spawn work.


def test_spawn_current_chat_precedes_boundary_but_not_mismatch():
    row = SpawnRecord(
        id="p1", status="running", chat_id="c-old", current_chat_id="c-new"
    )
    assert row.continue_chat_id == "c-new"
    terminal = row.model_copy(update={"status": "succeeded"})
    assert terminal.continue_chat_id == "c-new"
    mismatch = row.model_copy(update={
        "run_boundary": RunBoundaryOutcome(status="mismatch"),
    })
    assert mismatch.continue_chat_id == "c-old"
