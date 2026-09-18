"""Native Pi journal content agrees across shared readers and disposable previews."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.transcript_preview import TRANSCRIPT_PREVIEW_VERSION
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_index import SessionIndexInput, session_index_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.history import HarnessHistoryWriter, ingest_portable_history
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def test_pi_native_retained_and_zip_journal_readback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    events = [
        {"type": "session", "version": 3, "id": "pi-session", "cwd": str(project)},
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "first question"},
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {"role": "assistant", "content": "first answer"},
        },
        {
            "type": "compaction",
            "id": "c",
            "parentId": "b",
            "summary": "kept handoff",
            "firstKeptEntryId": "b",
            "tokensBefore": 10,
        },
        {
            "type": "branch_summary",
            "id": "d",
            "parentId": "a",
            "fromId": "c",
            "summary": "branchneedle",
        },
        {
            "type": "message",
            "id": "e",
            "parentId": "d",
            "message": {"role": "assistant", "content": "second answer"},
        },
    ]
    native = tmp_path / "native.jsonl"
    native.write_text("".join(json.dumps(event) + "\n" for event in events))
    original = native.read_bytes()
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="first question", harness="pi", model="test", agent="coder"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(root, key, iter(events))
    for kwargs in ({"file_path": str(native)}, {"ref": key}):
        log = session_log_sync(SessionLogInput(project_root=str(project), full=True, **kwargs))
        assert log.total_segments == 2
        assert any(
            entry.kind == "annotation" and "branchneedle" in entry.content for entry in log.entries
        )
        assert "kept handoff" in log.format_text()
        exported = session_export_sync(SessionExportInput(project_root=str(project), **kwargs))
        assert all(
            text in exported.markdown
            for text in ("first answer", "second answer", "branchneedle", "kept handoff")
        )
        search = session_search_sync(
            SessionSearchInput(project_root=str(project), query="branchneedle", **kwargs)
        )
        assert search.complete and search.matches
        assert search.matches[0].role == "annotation"
    record = spawn_store.get_spawn(root, key)
    assert record is not None
    archived = archive_history(root, destination=tmp_path / "archives", refs=(key,), apply=True)
    assert archived.reclaimed
    log = session_log_sync(
        SessionLogInput(project_root=str(project), ref=str(record.history_id), full=True)
    )
    assert any("branchneedle" in entry.content for entry in log.entries)
    assert native.read_bytes() == original


def test_parser_upgrade_invalidates_empty_preview_and_counts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="question", harness="pi", model="test", agent="coder"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root,
        key,
        iter([{"type": "message", "message": {"role": "assistant", "content": "PI_VISIBLE"}}]),
    )
    record = spawn_store.get_spawn(root, key)
    assert record is not None
    identity = PreviewIdentity(key, str(record.history_id))
    reader = SessionPreview(str(project))
    view = reader.refresh(identity, lambda: True)
    assert view is not None and "PI_VISIBLE" in view.lines
    index = HistoryIndex(root)
    with sqlite3.connect(index.path) as db:
        value = json.loads(
            db.execute("SELECT value FROM previews WHERE key=?", (identity.key,)).fetchone()[0]
        )
        value["preview"].update(
            version=TRANSCRIPT_PREVIEW_VERSION - 1, messages=[], has_interaction=False
        )
        db.execute("UPDATE previews SET value=? WHERE key=?", (json.dumps(value), identity.key))
    assert reader.peek(identity) is None
    assert index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION) == 0
    assert session_index_sync(SessionIndexInput(project_root=str(project))).preview_cached == 0
    refreshed = reader.refresh(identity, lambda: True)
    assert refreshed is not None and "PI_VISIBLE" in refreshed.lines
    assert index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION) == 1


def test_unsupported_pi_material_surfaces_incomplete_rendering(tmp_path: Path) -> None:
    path = tmp_path / "native.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "session", "version": 3, "id": "s", "cwd": str(tmp_path)},
                {"type": "future_message", "id": "a", "parentId": None, "content": "unrendered"},
            ]
        )
        + "\n"
    )
    log = session_log_sync(SessionLogInput(file_path=str(path), full=True))
    assert "rendering is incomplete" in log.format_text()
    search = session_search_sync(SessionSearchInput(file_path=str(path), query="unrendered"))
    assert not search.complete and search.errors
    export = session_export_sync(SessionExportInput(file_path=str(path)))
    assert "rendering is incomplete" in export.markdown


def test_unsupported_rendering_stays_visible_after_archiving(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="question", harness="pi", model="test", agent="coder"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root,
        key,
        iter(
            [
                {"type": "session", "id": "s", "version": 3, "cwd": str(project)},
                {"type": "future_message", "id": "a", "parentId": None},
            ]
        ),
    )
    record = spawn_store.get_spawn(root, key)
    assert record is not None
    identity = PreviewIdentity(key, str(record.history_id))
    reader = SessionPreview(str(project))
    loose = reader.refresh(identity, lambda: True)
    assert loose is not None and loose.state == "unavailable"
    result = archive_history(root, destination=tmp_path / "archives", refs=(key,), apply=True)
    assert result.reclaimed
    archived = reader.refresh(identity, lambda: True)
    assert archived is not None and archived.state == "unavailable"
    assert archived.lines == loose.lines
    assert HistoryIndex(root).preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION) == 0


def test_pi_preview_preserves_branch_context_after_append(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="question", harness="pi", model="test", agent="coder"
    )
    writer = HarnessHistoryWriter(
        root / "spawns" / key / "history.jsonl", runtime_root=root, spawn_id=key
    )
    for payload in [
        {"type": "session", "version": 3, "id": "s", "cwd": str(project)},
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": "first question"},
        },
        {"type": "model_change", "id": "b", "parentId": "a", "modelId": "test"},
    ]:
        assert writer.write(RawHarnessEvent("retained/native", payload, "pi")).success
    record = spawn_store.get_spawn(root, key)
    assert record is not None
    identity = PreviewIdentity(key, str(record.history_id))
    first = SessionPreview(str(project)).refresh(identity, lambda: True)
    assert first is not None and first.state == "current"
    assert writer.write(
        RawHarnessEvent(
            "retained/native",
            {
                "type": "message",
                "id": "c",
                "parentId": "a",
                "message": {"role": "assistant", "content": "branch answer"},
            },
            "pi",
        )
    ).success
    resumed = SessionPreview(str(project)).refresh(identity, lambda: True)
    assert resumed is not None and resumed.state == "current"
    assert resumed.lines.count("first question") == resumed.lines.count("branch answer") == 1
    assert sum("parent changed" in line for line in resumed.lines) == 1


def test_rebuild_warms_archived_children_and_counts_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    keys: list[str] = []
    for supported in (True, False):
        key = spawn_store.start_spawn(
            root, chat_id="c1", prompt="question", harness="pi", model="test", agent="coder"
        )
        keys.append(key)
        spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
        ingest_portable_history(
            root,
            key,
            iter(
                [
                    {"type": "session", "version": 3, "id": "s", "cwd": str(project)},
                    {
                        "type": "message" if supported else "future_message",
                        "id": "a",
                        "parentId": None,
                        "message": {"role": "assistant", "content": "archive warm content"},
                    },
                ]
            ),
        )
    archived = archive_history(
        root, destination=tmp_path / "archives", refs=tuple(keys), apply=True
    )
    assert len(archived.reclaimed) == 2
    metadata = session_index_sync(
        SessionIndexInput(
            project_root=str(project),
            action="rebuild",
            metadata_only=True,
        )
    )
    assert metadata.preview_cached == 0
    warmed = session_index_sync(SessionIndexInput(project_root=str(project), action="rebuild"))
    assert warmed.preview_cached == 1
    assert warmed.preview_unavailable == 1
