"""Retained snapshots read only when an import ref or restored record selects them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

from meridian.lib.ops.session_archive import (
    SessionImportInput,
    archive_history,
    materialize_native_history,
    session_import_sync,
)
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.ops.session_target import resolve_transcript_source
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import LAUNCH_FILE_NAMES, resolve_project_runtime_root_for_write

NEEDLE = "SNAPNEEDLE"


class Archived(NamedTuple):
    zip: Path
    history_id: str
    spawn_id: str
    native: Path
    root: Path
    project: Path


def _project(tmp_path: Path, name: str) -> tuple[Path, Path]:
    project = tmp_path / name
    project.mkdir()
    return project, resolve_project_runtime_root_for_write(project)


@pytest.fixture
def archived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Archived:
    """Runtime A: one bound Pi chat, captured and reclaimed into a ZIP."""
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project, root = _project(tmp_path, "a")
    key = spawn_store.start_spawn(
        root, chat_id="c1", prompt="question", harness="pi", model="test", agent="coder"
    ).spawn_id
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    sid = f"native-{key}"
    store = tmp_path / "native"
    store.mkdir()
    native = store / f"timestamp_{sid}.jsonl"
    events = [
        {"type": "session", "version": 3, "id": sid, "cwd": str(project)},
        {
            "type": "message",
            "id": "u",
            "parentId": None,
            "message": {"role": "user", "content": "q"},
        },
        {
            "type": "message",
            "id": "a",
            "parentId": "u",
            "message": {"role": "assistant", "content": NEEDLE, "provider": "t", "model": "t"},
        },
    ]
    native.write_text("".join(json.dumps(event) + "\n" for event in events))
    session_store.start_session(
        root,
        harness="pi",
        harness_session_id=sid,
        native_store=str(store),
        model="test",
        chat_id="c1",
        kind="primary",
        spawn_id=key,
    )
    session_store.stop_session(root, "c1")
    materialize_native_history(project, root, key)
    row = spawn_store.get_spawn(root, key)
    assert row is not None and row.history_id is not None
    result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert result.reclaimed == (str(row.history_id),)
    return Archived(Path(result.archives[0]), str(row.history_id), key, native, root, project)


def _log(project: Path, ref: str) -> str:
    return session_log_sync(
        SessionLogInput(project_root=str(project), ref=ref, full=True)
    ).format_text()


def _export(project: Path, ref: str) -> str:
    return session_export_sync(SessionExportInput(project_root=str(project), ref=ref)).markdown


def test_imported_history_reads_zip_member_in_place(archived: Archived, tmp_path: Path) -> None:
    archived.native.unlink()
    project, root = _project(tmp_path, "c")
    imported = session_import_sync(
        SessionImportInput(project_root=str(project), archive=str(archived.zip))
    )
    assert imported.selected == (archived.history_id,)

    target = resolve_transcript_source(archived.history_id, project_root=project, runtime_root=root)
    assert target.source.kind == "snapshot" and target.source.path == archived.zip
    assert target.source.manifest_sha256
    assert NEEDLE in _log(project, archived.history_id)
    assert NEEDLE in _export(project, archived.history_id)
    search = session_search_sync(
        SessionSearchInput(project_root=str(project), ref=archived.history_id, query=NEEDLE)
    )
    assert search.complete and search.matches
    assert not any((root / "spawns").glob("*/" + LAUNCH_FILE_NAMES.native_snapshot))

    again = session_import_sync(
        SessionImportInput(project_root=str(project), archive=str(archived.zip))
    )
    assert again.selected == () and again.already_imported == (archived.history_id,)
    assert "Already imported: " + archived.history_id in again.format_text()
