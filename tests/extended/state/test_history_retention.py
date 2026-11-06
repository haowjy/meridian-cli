"""File-authority recovery, exact ZIP coverage, and inert retry contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state import spawn_store
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.retention_archive import (
    archive_locations,
    read_receipts,
    selected_snapshot_receipts,
    verify_archive,
)
from meridian.lib.state.retention_restore import restore_archive
from tests.support.launch_paths import launch_paths


def _selected_archive_locations(index: HistoryIndex, history_id: str, *, destination=None):
    receipts = selected_snapshot_receipts(read_receipts(index.root), UUID(history_id))
    return archive_locations(receipts, destination=destination)


def _terminal(root: Path) -> str:
    from meridian.lib.state.history_codec import transcript_header
    from meridian.lib.state.native_snapshot import (
        SnapshotHeader,
        SnapshotObservation,
        SnapshotRecord,
        SourceRevision,
        write_snapshot,
    )

    native_id = f"native-{root.name}"
    key = str(
        spawn_store.start_spawn(
            root,
            chat_id="c1",
            model="test",
            agent="coder",
            harness="codex",
            harness_session_id=native_id,
            prompt="hello",
        ).spawn_id
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "portable needle"}]},
        }
    ]
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    raw_records = [json.dumps(event, ensure_ascii=False) for event in events]
    records = tuple(
        SnapshotRecord(source="codex", ordinal=index, raw=raw)
        for index, raw in enumerate(raw_records)
    )
    header = SnapshotHeader(
        transcript=transcript_header(state, root.name),
        session_instance_id=state.session_instance_id,
        harness="codex",
        native_session_id=native_id,
        dialect="codex.rollout.v1",
        scope="fixture",
        observed_from=state.started_at or "2026-01-01T00:00:00+00:00",
    )
    observation = SnapshotObservation(
        observed_until=(state.terminal.finished_at if state.terminal else state.started_at)
        or "2026-01-01T00:00:01+00:00",
        sources=(
            SourceRevision(
                source="codex",
                sha256=hashlib.sha256("".join(raw_records).encode()).hexdigest(),
                records=len(records),
            ),
        ),
    )
    snapshot = launch_paths(root, key).native_transcript
    with snapshot.open("wb") as handle:
        write_snapshot(handle, header, records, lambda: observation)
    return key


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
    with sqlite3.connect(HistoryIndex(root).path) as db:
        assert db.execute("SELECT archive_id FROM records").fetchone()[0]
    destination = tmp_path / "fresh"
    restored = restore_archive(destination, archive, (str(original.history_id),))
    assert restore_archive(destination, archive, (str(original.history_id),)) == restored
    state = spawn_store.get_spawn(destination, restored[0])
    assert state is not None
    assert state.history_id == original.history_id
    assert state.record_mode == "historical" and state.worker_pid is None
    assert state.session_instance_id is not None
    assert state.session_instance_id.startswith("historical-")
    assert state.runner_pid is None and state.harness_session_id is None
    history = launch_paths(destination, restored[0]).native_transcript
    assert json.loads(history.read_text().splitlines()[0])["transcript"]["history_id"] == str(
        original.history_id
    )
    with (history.parent / "unexpected.txt").open("w") as handle:
        handle.write("conflict")
    with pytest.raises(ValueError, match="content changed"):
        restore_archive(destination, archive, (str(original.history_id),))
    assert archive.exists()


def test_fork_history_is_frozen_before_source_chat_resumes(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "runtime"
    source = session_store.start_session(
        root, harness="codex", harness_session_id="first", model="test", kind="primary"
    ).chat_id
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
        ).spawn_id
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
        ).chat_id
        session_store.stop_session(root, source)
        session_store.start_session(
            root,
            harness="codex",
            harness_session_id="first",
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
        ).spawn_id
        session_store.update_session_spawn_id(root, source, resumed)
        child = spawn_store.start_spawn(
            root,
            chat_id=fork,
            model="test",
            agent="coder",
            harness="codex",
            prompt="fork",
            kind="primary",
        ).spawn_id
        state = spawn_store.get_spawn(root, child)
        assert state is not None
        assert state.forked_from_history_id == original.history_id
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
    launch_paths(root, key).starting_prompt.unlink()
    result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert not result.reclaimed and result.errors
    assert (root / "spawns" / key / "state.json").exists()


def test_inventory_excludes_reserved_atomic_capture_temps(tmp_path: Path) -> None:
    from meridian.lib.state.retention_archive import inventory

    root = tmp_path / "runtime"
    key = _terminal(root)
    directory = root / "spawns" / key
    launch = launch_paths(root, key).dir
    (launch / ".native-transcript.jsonl.deadbeef.tmp").write_text("partial snapshot")
    (launch / ".history.jsonl.deadbeef.tmp").write_text("partial history")
    (launch / "notes.tmp").write_text("unrelated temp content")
    names = {member.name for member in inventory(directory)}
    assert "launches/1/.native-transcript.jsonl.deadbeef.tmp" not in names
    assert "launches/1/.history.jsonl.deadbeef.tmp" not in names
    assert "launches/1/native-transcript.jsonl" in names and "state.json" in names
    # The exclusion is reserved-name specific, not a blanket .tmp suffix skip.
    assert "launches/1/notes.tmp" in names


def test_recent_session_activity_protects_old_transcript(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    root = tmp_path / "runtime"
    key = _terminal(root)
    path = root / "spawns" / key / "state.json"
    state = json.loads(path.read_text())
    state["started_at"] = "2020-01-01T00:00:00Z"
    state["terminal"]["finished_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(state))
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="recent", model="test", spawn_id=key
    ).chat_id
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
        ).spawn_id
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
    ).chat_id
    session_store.stop_session(root, chat)
    state = spawn_store.get_spawn(root, key)
    session = session_store.chat_record(root, chat)
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


def test_restore_conflicts_with_changed_native_session_metadata(tmp_path: Path) -> None:
    from meridian.lib.state import session_store
    from meridian.lib.state.retention_archive import capture_record, publish_archive

    root = tmp_path / "runtime"
    key = _terminal(root)
    chat = session_store.start_session(
        root, harness="codex", harness_session_id="native", model="test", spawn_id=key
    ).chat_id
    session_store.stop_session(root, chat)
    state = spawn_store.get_spawn(root, key)
    session = session_store.chat_record(root, chat)
    assert state is not None and state.terminal is not None and session is not None
    record = capture_record(
        root / "spawns" / key,
        state.model_copy(update={"prompt": None}),
        session,
        state.terminal.finished_at,
    )
    receipt = publish_archive(root, tmp_path / "zips", (record,))
    session_store.set_work_attachments(root, {chat: "changed-after-capture"})
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
            if entry.filename.endswith("/starting-prompt.md"):
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
    assert (
        _selected_archive_locations(HistoryIndex(root), result.selected[0])[0].receipt.archive_id
        is not None
    )
    assert verify_archive(Path(result.archives[0])).records
    assert not (root / "spawns" / key).exists()


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


def test_failed_retirement_sync_is_repaired_before_gc_and_ack(tmp_path: Path, monkeypatch) -> None:
    from meridian.lib.state import spawn_aggregate
    from meridian.lib.state.retention_archive import read_receipts, recover_archives

    root = tmp_path / "runtime"
    key = _terminal(root)
    destination = tmp_path / "zips"
    sync = spawn_aggregate.fsync_directory

    def fail_source_parent(path: Path):
        if path == root / "spawns":
            raise OSError("source parent sync unavailable")
        sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(spawn_aggregate, "fsync_directory", fail_source_parent)
        result = archive_history(root, destination=destination, refs=(key,), apply=True)
        assert not result.reclaimed and result.errors
        retired = tuple((root / "spawns/.staging").iterdir())
        assert len(retired) == 1
        spawn_store.gc_abandoned_stages(root)
        assert retired[0].exists()
        assert recover_archives(root, destination)
        assert not any(receipt.event == "reclaimed" for receipt in read_receipts(root))
    spawn_store.gc_abandoned_stages(root)
    assert not retired[0].exists()
    assert not recover_archives(root, destination)
    assert any(receipt.event == "reclaimed" for receipt in read_receipts(root))
    assert verify_archive(Path(result.archives[0])).records


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
        _selected_archive_locations(index, str(state.history_id))
    assert (tmp_path / "older" / first.zip_name).exists()


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
        _selected_archive_locations(HistoryIndex(fresh), str(current.history_id))[0]
        .receipt.records[0]
        .state.work_id
        == "current-second"
    )
