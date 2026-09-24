"""Strict normalization for native resume/fork sources."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.ops.reference import (
    AuthorizedSourceMetadata,
    AuthorizedSourceUse,
    SourceMetadataUnavailable,
    SourceUseRefused,
    UntrackedSourceUse,
    resolve_authorized_source_metadata,
    resolve_source_use,
)
from meridian.lib.state import session_store, spawn_store
from tests.integration.ops.test_native_reference_authority import _pinned_journal


def test_chat_reference_returns_exact_authorized_source_and_original_ref(tmp_path: Path) -> None:
    locator = _pinned_journal(tmp_path)

    result = resolve_source_use(tmp_path, "resume", " c1 ")

    assert isinstance(result, AuthorizedSourceUse)
    assert result.original_ref == " c1 "
    assert result.source.ref.chat_id == "c1"
    assert result.source.locator == locator
    assert result.source.key.native_session_id == "conversation"
    assert result.lookup_scope == tmp_path
    assert result.authority is not None


def _linked_spawn_fixture(root: Path) -> tuple[AuthorizedSourceUse, LaunchPolicySnapshot]:
    _pinned_journal(root)
    admitted = resolve_source_use(root, "resume", "c1")
    assert isinstance(admitted, AuthorizedSourceUse)
    lifecycle = session_store.SessionRecord(
        chat_id="c1",
        kind="primary",
        harness="pi",
        harness_session_id="conversation",
        harness_session_ids=(),
        model="source-model",
        agent="agent",
        agent_path="",
        skills=(),
        skill_paths=(),
        params=(),
        started_at="2025-01-01T00:00:00Z",
        stopped_at=None,
        session_instance_id="generation-a",
        spawn_id="p1",
    )
    snapshot = LaunchPolicySnapshot(
        model="source-model", harness="pi", extra_args=("--from", "c1")
    )
    journal = replace(admitted.authority.journal, lifecycle={"c1": lifecycle})
    authority = session_store.NativeSourceUseSnapshot(journal)
    return replace(admitted, authority=authority), snapshot


def test_authorized_metadata_uses_only_lifecycle_linked_spawn_without_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    folds: list[int] = []
    read_journal = session_store.read_journal

    def count_folds(raw: bytes):
        folds.append(len(raw))
        return read_journal(raw)

    monkeypatch.setattr(session_store, "read_journal", count_folds)
    admitted, expected_snapshot = _linked_spawn_fixture(tmp_path)
    reads: list[tuple[str, bool]] = []

    def count_reads(root, spawn_id, *, include_prompt=True):
        reads.append((str(spawn_id), include_prompt))
        return rows[str(spawn_id)]

    rows = {
        "p1": spawn_store.SpawnRecord(
            id="p1",
            chat_id="c1",
            session_instance_id="generation-a",
            harness="pi",
            harness_session_id="conversation",
            launch_policy_snapshot=expected_snapshot,
        ),
        "p2": spawn_store.SpawnRecord(
            id="p2",
            chat_id="c1",
            session_instance_id="generation-a",
            harness="pi",
            harness_session_id="conversation",
            launch_policy_snapshot=LaunchPolicySnapshot(model="newer-model", harness="pi"),
        ),
    }

    monkeypatch.setattr(spawn_store, "get_spawn", count_reads)
    metadata = resolve_authorized_source_metadata(admitted)

    assert isinstance(metadata, AuthorizedSourceMetadata)
    assert metadata.launch_policy_snapshot == expected_snapshot
    assert metadata.lifecycle.spawn_id == "p1"
    assert metadata.spawn_state_revision >= 0
    assert reads == [("p1", False)]
    assert len(folds) == 1


def test_authorized_metadata_fails_closed_on_missing_or_inconsistent_linked_row(
    tmp_path: Path, monkeypatch
) -> None:
    admitted, _ = _linked_spawn_fixture(tmp_path)

    monkeypatch.setattr(spawn_store, "get_spawn", lambda *args, **kwargs: None)
    missing = resolve_authorized_source_metadata(admitted)
    assert missing == SourceMetadataUnavailable("c1", "linked_spawn_missing")

    wrong_store_admission = replace(admitted, lookup_scope=tmp_path / "other-store")
    wrong_scope = resolve_authorized_source_metadata(wrong_store_admission)
    assert wrong_scope == SourceMetadataUnavailable("c1", "linked_spawn_missing")


def test_authorized_metadata_checks_exact_native_key_before_spawn_read(tmp_path: Path) -> None:
    admitted, _ = _linked_spawn_fixture(tmp_path)
    other_store_key = admitted.source.key.model_copy(update={"store": "/native/other"})
    wrong_source = admitted.source.model_copy(update={"key": other_store_key})

    result = resolve_authorized_source_metadata(replace(admitted, source=wrong_source))

    assert result == SourceMetadataUnavailable("c1", "authority_mismatch")


def test_authorized_metadata_rejects_linked_row_generation_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    admitted, snapshot = _linked_spawn_fixture(tmp_path)
    monkeypatch.setattr(
        spawn_store,
        "get_spawn",
        lambda *args, **kwargs: spawn_store.SpawnRecord(
            id="p1",
            chat_id="c1",
            session_instance_id="older-generation",
            harness="pi",
            harness_session_id="conversation",
            launch_policy_snapshot=snapshot,
        ),
    )

    result = resolve_authorized_source_metadata(admitted)

    assert result == SourceMetadataUnavailable("c1", "linked_spawn_mismatch")


def test_authorized_metadata_rejects_conflicting_lifecycle_context(
    tmp_path: Path, monkeypatch
) -> None:
    admitted, snapshot = _linked_spawn_fixture(tmp_path)
    lifecycle = admitted.authority.journal.lifecycle["c1"].model_copy(
        update={
            "active_work_id": "work-a",
            "control_root": "/control-a",
            "task_cwd": "/task-a",
            "execution_cwd": "/exec-a",
        }
    )
    journal = replace(admitted.authority.journal, lifecycle={"c1": lifecycle})
    admitted = replace(
        admitted, authority=session_store.NativeSourceUseSnapshot(journal)
    )
    row = spawn_store.SpawnRecord(
        id="p1",
        chat_id="c1",
        session_instance_id="generation-a",
        harness="pi",
        harness_session_id="conversation",
        launch_policy_snapshot=snapshot,
        work_id="work-a",
        control_root="/control-a",
        task_cwd="/task-a",
        execution_cwd="/exec-a",
    )
    monkeypatch.setattr(spawn_store, "get_spawn", lambda *args, **kwargs: row)

    for field in ("work_id", "control_root", "task_cwd", "execution_cwd"):
        conflict = row.model_copy(update={field: "different"})
        monkeypatch.setattr(
            spawn_store, "get_spawn", lambda *args, row=conflict, **kwargs: row
        )
        result = resolve_authorized_source_metadata(admitted)
        assert result == SourceMetadataUnavailable(
            "c1", "linked_spawn_mismatch", field
        )

    absent = row.model_copy(
        update={
            "work_id": None,
            "control_root": None,
            "task_cwd": None,
            "execution_cwd": None,
        }
    )
    monkeypatch.setattr(spawn_store, "get_spawn", lambda *args, **kwargs: absent)
    result = resolve_authorized_source_metadata(admitted)
    assert isinstance(result, AuthorizedSourceMetadata)

    # Missing lifecycle context is also permitted; this join never fills it
    # from the row, but it does not reject otherwise-matching metadata.
    no_context_root = tmp_path / "no-context"
    no_context_root.mkdir()
    admitted_without_context, _ = _linked_spawn_fixture(no_context_root)
    monkeypatch.setattr(spawn_store, "get_spawn", lambda *args, **kwargs: row)
    result = resolve_authorized_source_metadata(admitted_without_context)
    assert isinstance(result, AuthorizedSourceMetadata)


def test_authorized_metadata_detaches_entire_policy_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    admitted, snapshot = _linked_spawn_fixture(tmp_path)
    snapshot = snapshot.model_copy(
        update={
            "env": {"STORE": "original"},
            "agent_profile": {"nested": {"value": "original"}},
            "selection_report": {"nested": {"value": "original"}},
            "field_provenance": {"model": "original"},
        }
    )
    row = spawn_store.SpawnRecord(
        id="p1",
        chat_id="c1",
        session_instance_id="generation-a",
        harness="pi",
        harness_session_id="conversation",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setattr(spawn_store, "get_spawn", lambda *args, **kwargs: row)

    retained = resolve_authorized_source_metadata(admitted)
    assert isinstance(retained, AuthorizedSourceMetadata)
    assert retained.admitted.authority is admitted.authority
    assert retained.launch_policy_snapshot is not snapshot

    snapshot.env["STORE"] = "changed"
    snapshot.agent_profile["nested"]["value"] = "changed"
    snapshot.selection_report["nested"]["value"] = "changed"
    snapshot.field_provenance["model"] = "changed"
    assert retained.launch_policy_snapshot.env == {"STORE": "original"}
    assert retained.launch_policy_snapshot.agent_profile == {
        "nested": {"value": "original"}
    }
    assert retained.launch_policy_snapshot.selection_report == {
        "nested": {"value": "original"}
    }
    assert retained.launch_policy_snapshot.field_provenance == {"model": "original"}


def test_invalid_utf8_linked_spawn_state_returns_typed_unavailable(tmp_path: Path) -> None:
    admitted, _snapshot = _linked_spawn_fixture(tmp_path)
    state_path = tmp_path / "spawns" / "p1" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"\xff")

    result = resolve_authorized_source_metadata(admitted)

    assert result == SourceMetadataUnavailable("c1", "linked_spawn_invalid")
    assert state_path.read_bytes() == b"\xff"


def test_bare_id_requires_clean_unique_v4_claim_and_harness_match(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)

    allowed = resolve_source_use(tmp_path, "fork", "conversation", "pi")
    mismatch = resolve_source_use(tmp_path, "resume", "conversation", "claude")

    assert isinstance(allowed, AuthorizedSourceUse)
    assert allowed.source.key.native_session_id == "conversation"
    assert mismatch == SourceUseRefused(
        "resume", "conversation", "harness_mismatch", "c1"
    )


def test_bare_id_refuses_legacy_contradiction_for_selected_chat(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    session_store.start_session(tmp_path, "pi", "other-id", "test", chat_id="c1")

    result = resolve_source_use(tmp_path, "resume", "conversation", "pi")

    assert isinstance(result, SourceUseRefused)
    assert result.reason == "native_claim_blocked"


def test_invalid_authority_is_not_an_untracked_negative(tmp_path: Path) -> None:
    (tmp_path / "sessions.jsonl").write_bytes(b'{"event":"start"')

    result = resolve_source_use(tmp_path, "resume", "unknown-native", "pi")

    assert result == SourceUseRefused(
        "resume", "unknown-native", "native_claim_unavailable"
    )


def test_untracked_bare_id_requires_complete_negative_lookup(tmp_path: Path) -> None:
    result = resolve_source_use(tmp_path, "fork", "genuinely-new", "pi")

    assert isinstance(result, UntrackedSourceUse)
    assert result.native_id == "genuinely-new"
    assert result.harness == "pi"
    assert result.lookup_scope == tmp_path


def test_p_spawn_without_terminal_attempt_correlation_refuses(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    spawn_store.start_spawn(
        tmp_path,
        chat_id="c1",
        model="test",
        agent="test",
        harness="pi",
        prompt="synthetic",
        spawn_id="p1",
        harness_session_id="new-native",
    )

    untracked = resolve_source_use(tmp_path, "resume", "p1", "pi")

    assert untracked == SourceUseRefused("resume", "p1", "tracked_run_unresolved")

    spawn_store.start_spawn(
        tmp_path,
        chat_id="c8",
        model="test",
        agent="test",
        harness="pi",
        prompt="synthetic",
        spawn_id="p2",
        harness_session_id="conversation",
    )
    tracked = resolve_source_use(tmp_path, "resume", "p2", "pi")
    assert tracked == SourceUseRefused("resume", "p2", "tracked_run_unresolved")


def test_exact_chat_ignores_unrelated_same_spelling_in_another_store(tmp_path: Path) -> None:
    from meridian.lib.state import session_authority as authority
    from tests.integration.state.test_session_authority_v4 import _accept, _begin, _fact, _file

    first = _pinned_journal(tmp_path)
    rows = (tmp_path / "sessions.jsonl").read_text().splitlines()
    begin = _begin("run2", "attempt2", store="/native/other")
    builder = authority._JournalBuilder()
    authority.fold_row(builder, begin)
    fact = _fact(
        "entry",
        _file("/native/other/conversation.jsonl", inode=20),
        session_id="conversation",
        key=authority.NativeSessionKey(
            harness="pi", store="/native/other", native_session_id="conversation"
        ),
    ).model_copy(update={"run_id": "run2", "attempt_id": "attempt2"})
    second = _accept(builder, fact, "c2")
    serialized = [*rows, begin.model_dump_json(), second.model_dump_json()]
    (tmp_path / "sessions.jsonl").write_text("\n".join(serialized) + "\n")

    selected = resolve_source_use(tmp_path, "resume", "c1")
    bare = resolve_source_use(tmp_path, "resume", "conversation")

    assert isinstance(selected, AuthorizedSourceUse)
    assert selected.source.locator == first
    assert bare == SourceUseRefused("resume", "conversation", "native_claim_ambiguous")


def test_complete_final_row_without_lf_is_read_only_and_one_fold(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    journal = tmp_path / "sessions.jsonl"
    journal.write_bytes(journal.read_bytes().rstrip(b"\n"))
    before = journal.read_bytes()
    calls: list[int] = []
    original = session_store.read_journal

    def count(raw: bytes):
        calls.append(len(raw))
        return original(raw)

    with patch.object(session_store, "read_journal", count):
        chat = resolve_source_use(tmp_path, "resume", "c1")
        bare = resolve_source_use(tmp_path, "resume", "conversation")

    assert isinstance(chat, AuthorizedSourceUse)
    assert isinstance(bare, AuthorizedSourceUse)
    assert journal.read_bytes() == before
    assert calls == [len(before), len(before)]


def test_authority_fsync_failure_is_typed_refusal(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    for ref in ("c1", "conversation", "unlisted-native"):
        with patch.object(
            session_store, "_confirm_sessions_durability", side_effect=OSError("fsync")
        ):
            result = resolve_source_use(tmp_path, "resume", ref)
        assert result == SourceUseRefused("resume", ref, "native_claim_unavailable")


def test_exact_chat_retains_cross_harness_lifecycle_contradiction(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    session_store.start_session(tmp_path, "claude", "conversation", "test", chat_id="c1")

    result = resolve_source_use(tmp_path, "resume", "c1")

    assert result == SourceUseRefused("resume", "c1", "native_claim_blocked", "c1")


def test_invalid_and_torn_authority_refuse_without_repair(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    journal = tmp_path / "sessions.jsonl"
    valid = journal.read_bytes()
    for suffix in (b'{"event":"bad"}\n', b'{"event":"native_attempt"'):
        raw = valid + suffix
        journal.write_bytes(raw)
        result = resolve_source_use(tmp_path, "resume", "unknown-native")
        assert result == SourceUseRefused(
            "resume", "unknown-native", "native_claim_unavailable"
        )
        assert journal.read_bytes() == raw
