"""Tracked display references never substitute runner history for native identity."""

import json
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store


def seed(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "rt"
    store = tmp_path / "native"
    store.mkdir()
    sid = "11111111-1111-4111-8111-111111111111"
    native = store / f"{sid}.jsonl"
    native.write_text(
        json.dumps(
            {
                "sessionId": sid,
                "type": "assistant",
                "message": {"role": "assistant", "content": "native text"},
            }
        )
        + "\n"
    )
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=sid,
        native_store=str(store),
        model="test",
        chat_id="c1",
    )
    spawn_store.start_spawn(
        root,
        spawn_id="p1",
        chat_id="c1",
        harness="claude",
        model="test",
        agent="test",
        prompt="test",
        harness_session_id=sid,
    )
    (root / "spawns/p1/history.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "runner text"}}
        )
        + "\n"
    )
    return root, native, sid


@pytest.mark.parametrize("ref", ["p1", "raw"])
def test_old_spawn_and_raw_id_choose_native(tmp_path: Path, ref: str) -> None:
    root, native, sid = seed(tmp_path)
    target = resolve_session_log_target(
        ref=sid if ref == "raw" else ref, file_path=None, project_root=tmp_path, runtime_root=root
    )
    assert target.file_path == native
    assert target.sources[0].kind == "native_file"


def test_bound_missing_never_falls_back(tmp_path: Path) -> None:
    root, native, _ = seed(tmp_path)
    native.unlink()
    with pytest.raises(NativeSessionUnavailable) as exc:
        resolve_session_log_target(
            ref="p1", file_path=None, project_root=tmp_path, runtime_root=root
        )
    assert exc.value.reason == "missing"


def test_shared_native_id_lists_other_chats(tmp_path: Path) -> None:
    root, native, sid = seed(tmp_path)
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=sid,
        native_store=str(native.parent),
        model="test",
        chat_id="c2",
    )
    target = resolve_session_log_target(
        ref=sid, file_path=None, project_root=tmp_path, runtime_root=root
    )
    assert target.file_path == native
    assert target.view_label == "also bound to c2"


@pytest.mark.parametrize(
    ("status", "boundary", "exit_chat", "label", "selected"),
    [
        ("running", None, None, "entry-based view (run in progress)", "entry"),
        ("running", "verified", "c2", "entry-based view (run in progress)", "entry"),
        ("succeeded", None, None, "entry chat (run predates exit tracking)", "entry"),
        ("succeeded", "verified", "c1", None, "entry"),
        ("succeeded", "verified", "c2", "exit chat c2", "exit"),
        ("succeeded", "unresolved", None, "entry-based view (exit identity unresolved)", "entry"),
        ("succeeded", "mismatch", None, "entry-based view (exit identity mismatch)", "entry"),
    ],
)
def test_spawn_view_matrix(tmp_path, status, boundary, exit_chat, label, selected):
    from meridian.lib.state.spawn.model import RunBoundaryOutcome

    root, native, _ = seed(tmp_path)
    exit_sid = "22222222-2222-4222-8222-222222222222"
    exit_file = native.parent / f"{exit_sid}.jsonl"
    exit_file.write_text(json.dumps({"sessionId": exit_sid}) + "\n")
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=exit_sid,
        native_store=str(native.parent),
        model="test",
        chat_id="c2",
    )
    if boundary:
        spawn_store.update_spawn(
            root,
            "p1",
            run_boundary=RunBoundaryOutcome(
                status=boundary,
                exit_chat_id=exit_chat,
            ),
        )
    if status == "succeeded":
        spawn_store.finalize_spawn(root, "p1", "succeeded", 0, origin="runner")
    target = resolve_session_log_target(ref="p1", project_root=tmp_path, runtime_root=root)
    assert target.file_path == (native if selected == "entry" else exit_file)
    assert target.view_label == label


def test_raw_id_in_two_stores_is_ambiguous(tmp_path):
    root, native, sid = seed(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / native.name).write_bytes(native.read_bytes())
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=sid,
        native_store=str(other),
        model="test",
        chat_id="c2",
    )
    with pytest.raises(NativeSessionUnavailable) as exc:
        resolve_session_log_target(ref=sid, project_root=tmp_path, runtime_root=root)
    assert exc.value.reason == "ambiguous_native_file"


@pytest.mark.parametrize("renamed", [False, True])
def test_explicit_legacy_file_is_rejected(tmp_path, renamed):
    from meridian.lib.ops.session_log import SessionLogInput, session_log_sync

    root, _, _ = seed(tmp_path)
    history = root / "spawns/p1/history.jsonl"
    if renamed:
        history = tmp_path / "renamed.jsonl"
        history.write_text(json.dumps({"record": "meridian.transcript", "version": 2}) + "\n")
    with pytest.raises(ValueError, match="not a native transcript"):
        session_log_sync(SessionLogInput(file_path=str(history)))


def test_unbound_chat_never_reads_legacy_bytes(tmp_path):
    root = tmp_path / "rt"
    session_store.start_session(
        root, harness="claude", harness_session_id="", model="test", chat_id="c1"
    )
    spawn_store.start_spawn(
        root,
        spawn_id="p1",
        chat_id="c1",
        harness="claude",
        model="test",
        agent="test",
        prompt="test",
    )
    (root / "spawns/p1/history.jsonl").write_text("{}\n")
    with pytest.raises(NativeSessionUnavailable) as exc:
        resolve_session_log_target(ref="p1", project_root=tmp_path, runtime_root=root)
    assert exc.value.reason == "unbound"


@pytest.mark.parametrize("ref", ["c1", "p1", "raw"])
def test_native_read_is_runner_history_blind(tmp_path, monkeypatch, ref):
    from meridian.lib.ops.session_transcript import SessionLogRoute, parse_session_target

    root, _, sid = seed(tmp_path)
    original = Path.open

    def open_without_history(path, *args, **kwargs):
        assert path.name != "history.jsonl", "implicit runner-history read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_without_history)
    target = resolve_session_log_target(
        ref=sid if ref == "raw" else ref, project_root=tmp_path, runtime_root=root
    )
    parsed = parse_session_target(
        project_root=tmp_path, runtime_root=root, target=target, route=SessionLogRoute("ref", ref)
    )
    assert parsed.entries[0].content == "native text"


def test_spawn_without_chat_is_unbound(tmp_path):
    root = tmp_path / "rt"
    spawn_store.start_spawn(
        root,
        spawn_id="p1",
        chat_id=None,
        harness="claude",
        model="test",
        agent="test",
        prompt="test",
    )
    with pytest.raises(NativeSessionUnavailable) as exc:
        resolve_session_log_target("p1", project_root=tmp_path, runtime_root=root)
    assert exc.value.reason == "unbound"


def test_opencode_preview_refreshes_recorded_database_wal(tmp_path):
    import sqlite3

    from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview
    from meridian.lib.state.paths import resolve_project_runtime_root_for_write
    from tests.support.opencode_db import write_opencode_db_session_with_parts

    project = tmp_path / "project"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    database = tmp_path / "recorded.db"
    sid = "ses_native_preview"
    write_opencode_db_session_with_parts(
        db_path=database,
        session_id=sid,
        messages=[("assistant", {}, [{"type": "text", "text": "before WAL update"}])],
    )
    session_store.start_session(
        root,
        harness="opencode",
        harness_session_id=sid,
        native_store=str(database),
        model="test",
        chat_id="c1",
    )
    record = session_store.get_session_record(root, "c1")
    assert record is not None
    identity = PreviewIdentity("c1", generation=record.session_instance_id or sid)
    from meridian.lib.state.history_index import HistoryIndex

    HistoryIndex(root).rebuild()
    reader = SessionPreview(str(project))
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        before = reader.refresh(identity, lambda: True)
        assert before is not None and "before WAL update" in before.lines
        assert reader.peek(identity) is not None
        stamp = database.stat().st_mtime_ns
        db.execute("UPDATE part SET data = replace(data, 'before WAL update', 'after WAL update')")
        db.commit()
        assert database.stat().st_mtime_ns == stamp
        after = reader.refresh(identity, lambda: True)
        assert after is not None and "after WAL update" in after.lines
