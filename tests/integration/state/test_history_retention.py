"""File-authority recovery, exact ZIP coverage, and inert retry contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state import spawn_store
from meridian.lib.state.history import ingest_portable_history
from meridian.lib.state.history_changes import HistoryChanges
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.retention_archive import verify_archive
from meridian.lib.state.retention_restore import restore_archive


def _terminal(root: Path) -> str:
    key = str(
        spawn_store.start_spawn(
            root, chat_id="c1", model="test", agent="coder", harness="codex", prompt="hello"
        )
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root,
        key,
        iter(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "portable needle"}]},
                }
            ]
        ),
    )
    return key


def test_rebuild_and_missed_metadata_write_are_discovered(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    index = HistoryIndex(root)
    assert index.rebuild().complete
    spawn_store.update_spawn(root, key, work_id="changed")
    assert [row.id for row in index.spawns(work_id="changed")] == [key]
    assert index.rebuild(reset=True).complete
    assert [row.id for row in index.spawns(work_id="changed")] == [key]
    assert HistoryChanges(root).capture()[1] == ()


def test_zip_transfer_restore_is_inert_repeatable_and_conflict_safe(tmp_path: Path) -> None:
    root = tmp_path / "source"
    key = _terminal(root)
    original = spawn_store.get_spawn(root, key)
    assert original is not None
    output = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert output.reclaimed == (str(original.history_id),)
    assert not (root / "spawns" / key).exists()
    archive = Path(output.archives[0])
    assert verify_archive(archive).records[0].history_id == original.history_id
    assert HistoryIndex(root).rebuild().complete
    with HistoryIndex(root).query() as db:
        assert db.execute("SELECT archive_id FROM records").fetchone()[0]
    destination = tmp_path / "fresh"
    restored = restore_archive(destination, archive, (str(original.history_id),))
    assert restore_archive(destination, archive, (str(original.history_id),)) == restored
    state = spawn_store.get_spawn(destination, restored[0])
    assert state is not None
    assert state.history_id == original.history_id
    assert state.record_mode == "historical" and state.worker_pid is None
    assert state.runner_pid is None and state.harness_session_id is None
    history = destination / "spawns" / restored[0] / "history.jsonl"
    assert json.loads(history.read_text().splitlines()[0])["history_id"] == str(original.history_id)
    with (history.parent / "unexpected.txt").open("w") as handle:
        handle.write("conflict")
    with pytest.raises(ValueError, match="content changed"):
        restore_archive(destination, archive, (str(original.history_id),))
    assert archive.exists()


def test_committed_projection_survives_lost_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    index = HistoryIndex(root)
    index.rebuild()
    spawn_store.update_spawn(root, key, work_id="after-crash")

    def lose_ack(*args, **kwargs):
        raise RuntimeError("crash after durable projection")

    with monkeypatch.context() as patch:
        patch.setattr(HistoryChanges, "acknowledge", lose_ack)
        with pytest.raises(RuntimeError, match="crash after"):
            index.catch_up()
    assert [row.id for row in index.spawns(work_id="after-crash")] == [key]
    assert not HistoryChanges(root).capture()[1]


def test_rebuild_recovers_confirmed_corrupt_index_without_losing_authority(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    index = HistoryIndex(root)
    index.rebuild()
    # Offline corruption: no open SQLite connection or held database lock.
    index.path.write_bytes(b"not a sqlite database")
    assert index.rebuild().complete
    assert [row.id for row in index.spawns()] == [key]
    assert list(index.directory.glob("corrupt-*.sqlite3"))


def test_transferred_native_content_renders_without_harness_storage(tmp_path: Path) -> None:
    from meridian.lib.harness.transcript import parse_transcript_file

    root = tmp_path / "runtime"
    key = _terminal(root)
    copied = tmp_path / "transferred.jsonl"
    copied.write_bytes((root / "spawns" / key / "history.jsonl").read_bytes())
    segments, _ = parse_transcript_file(copied)
    assert any("portable needle" in message.content for segment in segments for message in segment)


def test_offline_latest_archive_does_not_hide_available_copy(tmp_path: Path) -> None:
    import shutil

    from meridian.lib.state.retention_archive import import_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    row = spawn_store.get_spawn(root, key)
    assert row is not None
    result = archive_history(root, destination=tmp_path / "first", refs=(key,), apply=True)
    first = Path(result.archives[0])
    second = tmp_path / "second"
    second.mkdir()
    shutil.copyfile(first, second / first.name)
    import_archive(root, second / first.name)
    index = HistoryIndex(root)
    assert index.read_targets(str(row.history_id))[0].path == second / first.name
    second.rename(tmp_path / "unmounted")
    assert index.read_targets(str(row.history_id))[0].path == first
    assert index.rebuild().complete
    assert index.read_targets(str(row.history_id))[0].path == first


def test_fork_history_is_frozen_before_source_chat_resumes(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "runtime"
    source = session_store.start_session(
        root, harness="codex", harness_session_id="first", model="test", kind="primary"
    )
    fork = None
    try:
        first = spawn_store.start_spawn(
            root,
            chat_id=source,
            model="test",
            agent="coder",
            harness="codex",
            prompt="first",
            kind="primary",
        )
        session_store.update_session_spawn_id(root, source, first)
        original = spawn_store.get_spawn(root, first)
        assert original is not None
        fork = session_store.start_session(
            root,
            harness="codex",
            harness_session_id="fork",
            model="test",
            kind="primary",
            forked_from_chat_id=source,
        )
        session_store.stop_session(root, source)
        session_store.start_session(
            root,
            harness="codex",
            harness_session_id="resumed",
            model="test",
            kind="primary",
            chat_id=source,
        )
        resumed = spawn_store.start_spawn(
            root,
            chat_id=source,
            model="test",
            agent="coder",
            harness="codex",
            prompt="resumed",
            kind="primary",
        )
        session_store.update_session_spawn_id(root, source, resumed)
        child = spawn_store.start_spawn(
            root,
            chat_id=fork,
            model="test",
            agent="coder",
            harness="codex",
            prompt="fork",
            kind="primary",
        )
        state = spawn_store.get_spawn(root, child)
        assert state is not None
        assert state.forked_from_history_id == original.history_id
        exact = session_store.resolve_session_ref(root, "first")
        assert exact is not None and exact.history_id == original.history_id
    finally:
        session_store.stop_session(root, source)
        if fork:
            session_store.stop_session(root, fork)


def test_mismatched_local_identity_cannot_redirect_record_mutation(tmp_path: Path) -> None:
    from meridian.lib.state.spawn.repository import SpawnStateQuarantined

    key = _terminal(tmp_path)
    path = tmp_path / "spawns" / key / "state.json"
    value = json.loads(path.read_text())
    value["id"] = "p999"
    path.write_text(json.dumps(value))
    with pytest.raises(SpawnStateQuarantined):
        spawn_store.update_spawn(tmp_path, key, work_id="redirected")
    assert not (tmp_path / "spawns/p999").exists()


def test_missing_referenced_prompt_prevents_reclaim(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    (root / "spawns" / key / "starting-prompt.md").unlink()
    result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert not result.reclaimed and result.errors
    assert (root / "spawns" / key / "state.json").exists()


def test_unavailable_current_digest_never_falls_back_to_older_snapshot(tmp_path: Path) -> None:
    from meridian.lib.state.retention_archive import capture_record, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    old = capture_record(
        root / "spawns" / key,
        state.model_copy(update={"prompt": None}),
        None,
        state.terminal.finished_at,
    )
    first = publish_archive(root, tmp_path / "older", (old,))
    spawn_store.update_spawn(root, key, work_id="newer snapshot")
    archive_history(root, destination=tmp_path / "current", refs=(key,), apply=True)
    index = HistoryIndex(root)
    assert len(index.snapshots()) == 2
    (tmp_path / "current").rename(tmp_path / "offline")
    with pytest.raises(FileNotFoundError):
        index.read_targets(str(state.history_id))
    assert (tmp_path / "older" / first.zip_name).exists()


def test_published_snapshot_is_reused_after_reclaim_interruption(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    destination = tmp_path / "zips"

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before loose deletion")

    with monkeypatch.context() as patch:
        patch.setattr(spawn_store, "delete_published_spawn", interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            archive_history(root, destination=destination, refs=(key,), apply=True)
    assert len(list(destination.glob("*.zip"))) == 1
    result = archive_history(root, destination=destination, refs=(key,), apply=True)
    assert len(result.reclaimed) == 1
    assert len(list(destination.glob("*.zip"))) == 1


def test_session_recovery_metadata_participates_in_portable_digest(tmp_path: Path) -> None:
    from meridian.lib.state import session_store
    from meridian.lib.state.retention_archive import capture_record

    root = tmp_path / "runtime"
    key = _terminal(root)
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="native", model="test", spawn_id=key
    )
    session_store.stop_session(root, chat)
    state = spawn_store.get_spawn(root, key)
    session = session_store.get_session_record(root, chat)
    assert state is not None and session is not None
    one = capture_record(
        root / "spawns" / key,
        state,
        session.model_copy(update={"active_work_id": "one"}),
        state.terminal.finished_at,
    )
    two = capture_record(
        root / "spawns" / key,
        state,
        session.model_copy(update={"active_work_id": "two"}),
        state.terminal.finished_at,
    )
    assert one.portable_digest != two.portable_digest


def test_recent_session_activity_protects_old_transcript(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "runtime"
    key = _terminal(root)
    path = root / "spawns" / key / "state.json"
    state = json.loads(path.read_text())
    state["started_at"] = "2020-01-01T00:00:00Z"
    state["terminal"]["finished_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(state))
    history = path.with_name("history.jsonl")
    lines = [json.loads(line) for line in history.read_text().splitlines()]
    for line in lines[1:]:
        line["timestamp"] = "2020-01-01T00:00:00Z"
    history.write_text("".join(json.dumps(line) + "\n" for line in lines))
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="recent", model="test", spawn_id=key
    )
    session_store.stop_session(root, chat)
    HistoryIndex(root).rebuild()
    result = archive_history(root, destination=tmp_path / "zips", eligible=True, apply=True)
    assert not result.selected and not result.reclaimed
    assert path.exists()


def test_unsafe_transitive_dependency_protects_dependent(tmp_path: Path) -> None:
    from uuid import uuid4

    root = tmp_path / "runtime"
    dependency = _terminal(root)
    dependent = str(
        spawn_store.start_spawn(
            root, chat_id="c2", model="test", agent="coder", harness="codex", prompt="dependent"
        )
    )
    spawn_store.finalize_spawn(root, dependent, status="succeeded", exit_code=0, origin="runner")
    row = spawn_store.get_spawn(root, dependency)
    assert row is not None
    from meridian.lib.state.spawn.repository import write_state_locked

    write_state_locked(
        root / "spawns",
        dependency,
        lambda record: record.model_copy(update={"retained_history_ids": (uuid4(),)}),
        allow_terminal_overwrite=True,
    )
    write_state_locked(
        root / "spawns",
        dependent,
        lambda record: record.model_copy(update={"retained_history_ids": (row.history_id,)}),
        allow_terminal_overwrite=True,
    )
    result = archive_history(root, destination=tmp_path / "zips", refs=(dependent,), apply=True)
    assert dependent in result.protected and dependency in result.protected
    assert not result.reclaimed


def test_restore_rejects_different_session_recovery_snapshot(tmp_path: Path) -> None:
    from meridian.lib.state import session_store
    from meridian.lib.state.retention_archive import capture_record, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="native", model="test", spawn_id=key
    )
    session_store.stop_session(root, chat)
    state = spawn_store.get_spawn(root, key)
    session = session_store.get_session_record(root, chat)
    assert state is not None and session is not None and state.terminal is not None
    paths = []
    for work in ("one", "two"):
        record = capture_record(
            root / "spawns" / key,
            state.model_copy(update={"prompt": None}),
            session.model_copy(update={"active_work_id": work}),
            state.terminal.finished_at,
        )
        receipt = publish_archive(root, tmp_path / work, (record,))
        paths.append(tmp_path / work / receipt.zip_name)
    restore_archive(tmp_path / "fresh", paths[0], (str(state.history_id),))
    with pytest.raises(ValueError, match="identity conflict"):
        restore_archive(tmp_path / "fresh", paths[1], (str(state.history_id),))
    assert all(path.exists() for path in paths)


def test_mounted_destination_hint_preserves_location_identity(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    key = _terminal(root)
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    destination = tmp_path / "mount-one"
    result = archive_history(root, destination=destination, refs=(key,), apply=True)
    destination.rename(tmp_path / "mount-two")
    targets = HistoryIndex(root).read_targets(
        str(state.history_id), destination=tmp_path / "mount-two"
    )
    assert targets[0].path == tmp_path / "mount-two" / Path(result.archives[0]).name


def test_restore_conflicts_with_changed_native_session_metadata(tmp_path: Path) -> None:
    from meridian.lib.state import session_store
    from meridian.lib.state.retention_archive import capture_record, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="native", model="test", spawn_id=key
    )
    session_store.stop_session(root, chat)
    state = spawn_store.get_spawn(root, key)
    session = session_store.get_session_record(root, chat)
    assert state is not None and state.terminal is not None and session is not None
    record = capture_record(
        root / "spawns" / key,
        state.model_copy(update={"prompt": None}),
        session,
        state.terminal.finished_at,
    )
    receipt = publish_archive(root, tmp_path / "zips", (record,))
    session_store.update_session_work_id(root, chat, "changed-after-capture")
    with pytest.raises(ValueError, match="identity conflict"):
        restore_archive(root, tmp_path / "zips" / receipt.zip_name, (str(state.history_id),))


@pytest.mark.parametrize("damage", ["traversal", "duplicate", "missing", "changed", "extra"])
def test_damaged_zip_cannot_publish_restore(tmp_path: Path, damage: str) -> None:
    import warnings
    import zipfile

    root = tmp_path / "runtime"
    key = _terminal(root)
    result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    original = Path(result.archives[0])
    damaged = tmp_path / f"{damage}.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, "w") as output:
        for entry in source.infolist():
            content = source.read(entry)
            if entry.filename.endswith("aggregate/starting-prompt.md"):
                if damage == "missing":
                    continue
                if damage == "changed":
                    content = b"wrong"
            output.writestr(entry, content)
        if damage == "traversal":
            output.writestr("../outside", "unsafe")
        elif damage == "duplicate":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                output.writestr(source.infolist()[0], source.read(source.infolist()[0]))
        elif damage == "extra":
            output.writestr("unlisted.txt", "unexpected")
    destination = tmp_path / "fresh"
    with pytest.raises(ValueError):
        restore_archive(destination, damaged, result.reclaimed)
    assert not (destination / "spawns").exists()
    assert verify_archive(original).records


def test_explicit_import_can_reselect_a_previously_imported_snapshot(tmp_path: Path) -> None:
    from meridian.lib.state.retention_archive import capture_record, import_archive, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    paths = []
    for work in ("one", "two"):
        spawn_store.update_spawn(root, key, work_id=work)
        state = spawn_store.get_spawn(root, key)
        assert state is not None and state.terminal is not None
        record = capture_record(
            root / "spawns" / key,
            state.model_copy(update={"prompt": None}),
            None,
            state.terminal.finished_at,
        )
        receipt = publish_archive(root, tmp_path / work, (record,))
        paths.append(tmp_path / work / receipt.zip_name)
    fresh = tmp_path / "fresh"
    import_archive(fresh, paths[0])
    import_archive(fresh, paths[1])
    import_archive(fresh, paths[0])
    target = HistoryIndex(fresh).read_targets(str(record.history_id))[0]
    assert target.state.work_id == "one"


def test_selective_restore_does_not_select_unrequested_snapshots(tmp_path: Path) -> None:
    from meridian.lib.state.retention_archive import capture_record, import_archive, publish_archive

    root = tmp_path / "runtime"
    keys = (_terminal(root), _terminal(root))
    records = []
    for key in keys:
        state = spawn_store.get_spawn(root, key)
        assert state is not None and state.terminal is not None
        records.append(
            capture_record(
                root / "spawns" / key,
                state.model_copy(update={"prompt": None}),
                None,
                state.terminal.finished_at,
            )
        )
    older = publish_archive(root, tmp_path / "older", tuple(records))
    spawn_store.update_spawn(root, keys[1], work_id="current-second")
    state = spawn_store.get_spawn(root, keys[1])
    assert state is not None and state.terminal is not None
    current = capture_record(
        root / "spawns" / keys[1],
        state.model_copy(update={"prompt": None}),
        None,
        state.terminal.finished_at,
    )
    newer = publish_archive(root, tmp_path / "newer", (current,))
    fresh = tmp_path / "fresh"
    import_archive(fresh, tmp_path / "newer" / newer.zip_name)
    restore_archive(fresh, tmp_path / "older" / older.zip_name, (str(records[0].history_id),))
    assert (
        HistoryIndex(fresh).read_targets(str(current.history_id))[0].state.work_id
        == "current-second"
    )


def test_missing_manifest_does_not_hide_healthy_equivalent_location(tmp_path: Path) -> None:
    import shutil
    import zipfile

    from meridian.lib.state.retention_archive import import_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    result = archive_history(root, destination=tmp_path / "first", refs=(key,), apply=True)
    original = Path(result.archives[0])
    second = tmp_path / "second"
    second.mkdir()
    copy = second / original.name
    shutil.copyfile(original, copy)
    import_archive(root, copy)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(copy, "w") as output:
        for entry in source.infolist():
            if not entry.filename.endswith("manifest.json"):
                output.writestr(entry, source.read(entry))
    assert HistoryIndex(root).read_targets(result.reclaimed[0])[0].path == original


def test_unavailable_prepared_archive_does_not_stall_unrelated_retention(
    tmp_path: Path, monkeypatch
) -> None:
    from meridian.lib.ops import session_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    append = session_archive.append_receipt

    def interrupt(root, receipt):
        if receipt.event == "reclaimed":
            raise RuntimeError("crash before reclaim completion receipt")
        append(root, receipt)

    with monkeypatch.context() as patch:
        patch.setattr(session_archive, "append_receipt", interrupt)
        with pytest.raises(RuntimeError):
            archive_history(root, destination=tmp_path / "first", refs=(key,), apply=True)
    (tmp_path / "first").rename(tmp_path / "offline")
    second = _terminal(root)
    result = archive_history(root, destination=tmp_path / "second", refs=(second,), apply=True)
    assert result.reclaimed and any("recovery deferred" in error for error in result.errors)
    assert not (root / "spawns" / second).exists()


def test_interrupted_recursive_reclaim_keeps_verified_zip_readable(
    tmp_path: Path, monkeypatch
) -> None:
    from meridian.lib.state import spawn_aggregate

    root = tmp_path / "runtime"
    key = _terminal(root)

    def partial_removal(directory: Path, **kwargs):
        (directory / "history.jsonl").unlink()
        raise OSError("interrupted recursive removal")

    with monkeypatch.context() as patch:
        patch.setattr(spawn_aggregate.shutil, "rmtree", partial_removal)
        result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert result.errors
    assert HistoryIndex(root).read_targets(result.selected[0])[0].archive_id is not None
    assert verify_archive(Path(result.archives[0])).records
    assert not (root / "spawns" / key).exists()


def test_child_binding_preserves_cross_owner_fork_and_native_identity(tmp_path: Path) -> None:
    from meridian.lib.core.spawn_start import SpawnStartMetadata
    from meridian.lib.ops.reference import _reference_from_session
    from meridian.lib.state import session_store

    root = tmp_path / "runtime"
    source_id = _terminal(root)
    source = spawn_store.get_spawn(root, source_id)
    assert source is not None
    child_id = spawn_store.start_spawn(
        root,
        chat_id="c99",
        owner_chat_id="c99",
        model="test",
        agent="coder",
        harness="codex",
        prompt="fork",
        kind="child",
        metadata=SpawnStartMetadata(forked_from_history_id=source.history_id),
    )
    # Reservation already protects the requested ancestor, before a session exists.
    assert not archive_history(
        root, destination=tmp_path / "zips", refs=(source_id,), apply=True
    ).reclaimed
    chat = session_store.start_session(
        root,
        harness="codex",
        harness_session_id="native-child",
        model="test",
        spawn_id=child_id,
        forked_from_history_id=source.history_id,
    )
    try:
        row = spawn_store.get_spawn(root, child_id)
        session = session_store.get_session_record(root, chat)
        assert row is not None and session is not None
        assert row.chat_id == chat and row.session_instance_id == session.session_instance_id
        assert row.forked_from_history_id == source.history_id
        assert session.history_id == row.history_id
        assert (
            _reference_from_session(root, session, tmp_path, "native-child").source_history_id
            == row.history_id
        )
        # Exact linked rows also repair references without a session UUID.
        assert (
            _reference_from_session(
                root, session.model_copy(update={"history_id": None}), tmp_path, "native-child"
            ).source_history_id
            == row.history_id
        )
    finally:
        session_store.stop_session(root, chat)


@pytest.mark.parametrize("with_session", [False, True])
def test_restored_snapshot_recapture_preserves_portable_digest(
    tmp_path: Path, with_session: bool
) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "source"
    key = _terminal(root)
    if with_session:
        chat = session_store.start_session(
            root, harness="codex", harness_session_id="native", model="test", spawn_id=key
        )
        session_store.stop_session(root, chat)
    original = archive_history(root, destination=tmp_path / "first", refs=(key,), apply=True)
    first = Path(original.archives[0])
    first_record = verify_archive(first).records[0]
    restored_root = tmp_path / "restored"
    # Force local aliases to differ from the origin aliases.
    _terminal(restored_root)
    restored = restore_archive(restored_root, first, (str(first_record.history_id),))
    second = archive_history(
        restored_root, destination=tmp_path / "second", refs=restored, apply=True
    )
    second_archive = Path(second.archives[0])
    second_record = verify_archive(second_archive).records[0]
    assert second_record.portable_digest == first_record.portable_digest
    assert second_record.required_files == first_record.required_files
    target = tmp_path / "target"
    first_alias = restore_archive(target, first, (str(first_record.history_id),))
    assert restore_archive(target, second_archive, (str(first_record.history_id),)) == first_alias


def test_historical_session_updates_are_refused(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "source"
    key = _terminal(root)
    output = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    fresh = tmp_path / "fresh"
    local = restore_archive(fresh, Path(output.archives[0]), output.reclaimed)[0]
    row = spawn_store.get_spawn(fresh, local)
    assert row is not None and row.chat_id is not None
    before = (fresh / "sessions.jsonl").read_bytes()
    with pytest.raises(ValueError, match="Historical"):
        session_store.update_session_work_id(fresh, row.chat_id, "changed")
    assert (fresh / "sessions.jsonl").read_bytes() == before
    event = json.loads(before)
    event["record"]["active_work_id"] = "foreign-edit"
    (fresh / "sessions.jsonl").write_text(json.dumps(event) + "\n")
    with pytest.raises(ValueError, match="metadata changed"):
        restore_archive(fresh, Path(output.archives[0]), output.reclaimed)


@pytest.mark.parametrize("field", ["spawn_id", "history_id"])
def test_restore_checks_nullable_session_identity_without_enrichment(
    tmp_path: Path, field: str
) -> None:
    root = tmp_path / "source"
    key = _terminal(root)
    output = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    archive = Path(output.archives[0])
    fresh = tmp_path / "fresh"
    local = restore_archive(fresh, archive, output.reclaimed)[0]
    event = json.loads((fresh / "sessions.jsonl").read_bytes())
    event["record"][field] = None
    (fresh / "sessions.jsonl").write_text(json.dumps(event) + "\n")
    with pytest.raises(ValueError, match="metadata changed"):
        restore_archive(fresh, archive, output.reclaimed)
    recapture = archive_history(fresh, destination=tmp_path / "second", refs=(local,), apply=True)
    assert not recapture.reclaimed
    assert recapture.errors and "metadata changed" in recapture.errors[0]
    assert (fresh / "spawns" / local / "state.json").exists()
    assert verify_archive(archive).records


def test_reclaim_orders_dependents_before_dependencies_and_preserves_unselected(tmp_path):
    from meridian.lib.state.spawn.repository import write_state_locked

    root = tmp_path / "runtime"
    parent, child = _terminal(root), _terminal(root)
    original = spawn_store.get_spawn(root, parent)
    assert original is not None
    write_state_locked(
        root / "spawns",
        child,
        lambda row: row.model_copy(
            update={"parent_id": parent, "parent_history_id": original.history_id}
        ),
        allow_terminal_overwrite=True,
    )
    only_parent = archive_history(root, destination=tmp_path / "zips", refs=(parent,), apply=True)
    assert not only_parent.reclaimed
    assert parent in only_parent.protected
    assert spawn_store.get_spawn(root, parent) is not None
    both = archive_history(root, destination=tmp_path / "zips", refs=(parent, child), apply=True)
    assert len(both.reclaimed) == 2
    assert spawn_store.get_spawn(root, parent) is None
    assert spawn_store.get_spawn(root, child) is None


def test_failed_archive_and_restore_stages_are_reclaimed_on_retry(tmp_path, monkeypatch):
    from meridian.lib.state import retention_restore
    from meridian.lib.state.retention_archive import capture_record, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    record = capture_record(root / "spawns" / key, state, None, state.started_at or "")
    (root / "spawns" / key / "changed.txt").write_text("changed")
    zips = tmp_path / "zips"
    with pytest.raises(ValueError, match="Source changed"):
        publish_archive(root, zips, (record,))
    assert not list(zips.glob(".partial-*"))
    archived = archive_history(root, destination=zips, refs=(key,), apply=True)
    archive = Path(archived.archives[0])
    destination = tmp_path / "restored"
    with monkeypatch.context() as patch:

        def fail_publication(*args, **kwargs):
            raise OSError("publication failed")

        patch.setattr(retention_restore, "atomic_publish_dir", fail_publication)
        with pytest.raises(OSError, match="publication failed"):
            restore_archive(destination, archive, (str(state.history_id),))
    assert not list((destination / "history-archives/staging").glob("restore-*"))
    assert list((destination / "history-archives/restores").glob("*.json"))
    restored = restore_archive(destination, archive, (str(state.history_id),))
    assert restore_archive(destination, archive, (str(state.history_id),)) == restored
    assert archive.exists()


def test_small_retention_passes_progress_through_dependency_chain(tmp_path):
    from meridian.lib.config.settings import HistoryArchiveConfig
    from meridian.lib.state.spawn.repository import write_state_locked

    root = tmp_path / "runtime"
    parent, child = _terminal(root), _terminal(root)
    original = spawn_store.get_spawn(root, parent)
    assert original is not None
    write_state_locked(
        root / "spawns",
        child,
        lambda row: row.model_copy(
            update={"parent_id": parent, "parent_history_id": original.history_id}
        ),
        allow_terminal_overwrite=True,
    )
    for expected in (child, parent):
        row = spawn_store.get_spawn(root, expected)
        assert row is not None
        result = archive_history(
            root,
            destination=tmp_path / "zips",
            eligible=True,
            after_days=0,
            apply=True,
            policy=HistoryArchiveConfig(max_records=1),
        )
        assert result.reclaimed == (str(row.history_id),)
