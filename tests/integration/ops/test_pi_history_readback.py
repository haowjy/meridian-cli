"""Native Pi journal content agrees across shared readers."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.session_archive import archive_history, materialize_native_history
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_index import SessionIndexInput, session_index_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _retain_bound_native(root, key, events):
    events = list(events)
    sid = f"native-{key}"
    if not events or events[0].get("type") != "session":
        events.insert(0, {"type": "session", "version": 3, "id": sid})
    events[0]["id"] = sid
    events[0].setdefault("cwd", str(root.parent))
    parent = None
    for i, event in enumerate(events[1:]):
        event.setdefault("id", f"entry-{i}")
        event.setdefault("parentId", parent)
        parent = event["id"]
        if event.get("type") == "message" and event["message"].get("role") == "assistant":
            event["message"].update(provider="test", model="test")
        if event.get("type") == "model_change":
            event["provider"] = "test"
    store = root.parent / "native" / key
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"timestamp_{sid}.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    row = spawn_store.get_spawn(root, key)
    assert row is not None
    session_store.start_session(
        root,
        harness="pi",
        harness_session_id=sid,
        native_store=str(store),
        model="test",
        chat_id=row.chat_id,
        kind="primary" if row.kind == "primary" else "spawn",
        spawn_id=row.id,
    )
    session_store.stop_session(root, row.chat_id)
    return path


def test_pi_native_readback_survives_reclaim(tmp_path: Path, monkeypatch) -> None:
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
    ).spawn_id
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    _retain_bound_native(root, key, iter(events))
    for kwargs in ({"file_path": str(native)}, {"ref": key}):
        log = session_log_sync(SessionLogInput(project_root=str(project), full=True, **kwargs))
        assert log.total_segments == (2 if "file_path" in kwargs else 1)
        assert any(
            entry.kind == "annotation" and "branchneedle" in entry.content for entry in log.entries
        )
        assert ("kept handoff" in log.format_text()) == ("file_path" in kwargs)
        exported = session_export_sync(SessionExportInput(project_root=str(project), **kwargs))
        assert all(text in exported.markdown for text in ("second answer", "branchneedle"))
        if "file_path" in kwargs:
            assert all(
                text in exported.markdown
                for text in ("first question", "first answer", "kept handoff")
            )
        search = session_search_sync(
            SessionSearchInput(project_root=str(project), query="branchneedle", **kwargs)
        )
        assert search.complete and search.matches
        assert search.matches[0].role == "annotation"
    record = spawn_store.get_spawn(root, key)
    assert record is not None
    materialize_native_history(project, root, key)
    archived = archive_history(root, destination=tmp_path / "archives", refs=(key,), apply=True)
    assert archived.reclaimed
    log = session_log_sync(
        SessionLogInput(project_root=str(project), ref=str(record.history_id), full=True)
    )
    assert any("branchneedle" in entry.content for entry in log.entries)
    assert native.read_bytes() == original


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
    assert search.complete and search.warnings
    export = session_export_sync(SessionExportInput(file_path=str(path)))
    assert "rendering is incomplete" in export.markdown


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
            root,
            chat_id="c1" if supported else "c2",
            prompt="question",
            harness="pi",
            model="test",
            agent="coder",
        ).spawn_id
        keys.append(key)
        spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
        _retain_bound_native(
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
    for key in keys:
        materialize_native_history(project, root, key)
    archived = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=tuple(keys),
        apply=True,
    )
    assert len(archived.reclaimed) == 2
    session_index_sync(
        SessionIndexInput(project_root=str(project), action="rebuild", metadata_only=True)
    )
    warmed = session_index_sync(SessionIndexInput(project_root=str(project), action="rebuild"))
    assert warmed.search_fresh == 2
    assert warmed.search_stale == 0
    assert warmed.search_unavailable == 1
