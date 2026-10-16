"""SQL-scoped no-argument spawn-wait discovery."""

import json
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.state.history_index as history_index
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.types import HarnessId
from meridian.lib.ops.session_archive import archive_history, materialize_native_history
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn.model import SpawnKind, SpawnRecord
from tests.support.resident_drain import start_row
from tests.support.spawn_runs import seed_run


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    root.mkdir(parents=True, exist_ok=True)
    return project, root


def _spawn(
    root: Path,
    name: str,
    *,
    chat: str = "c-owner",
    owner: str | None = "c-owner",
    parent: str | None = None,
    kind: SpawnKind = "child",
) -> str:
    return str(
        spawn_store.start_spawn(
            root,
            spawn_id=name,
            chat_id=chat,
            owner_chat_id=owner,
            parent_id=parent,
            parent_run_id=parse_ref(parent, row_key=True) if parent else None,
            model="test",
            agent="coder",
            harness="pi",
            kind=kind,
            prompt="wait discovery",
        )
    )


def _bind_pi_transcript(project: Path, root: Path, spawn_id: str) -> None:
    native_id = f"native-{spawn_id}"
    native_store = project.parent / "native" / spawn_id
    native_store.mkdir(parents=True)
    events = (
        {"type": "session", "version": 3, "id": native_id, "cwd": str(project)},
        {
            "type": "message",
            "id": "answer",
            "parentId": None,
            "message": {
                "role": "assistant",
                "content": "done",
                "provider": "test",
                "model": "test",
            },
        },
    )
    (native_store / f"timestamp_{native_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    row = spawn_store.get_spawn(root, spawn_id)
    assert row is not None and row.chat_id is not None
    session_store.start_session(
        root,
        harness="pi",
        harness_session_id=native_id,
        native_store=str(native_store),
        model="test",
        chat_id=row.chat_id,
        kind="spawn",
        spawn_id=row.id,
    )
    session_store.stop_session(root, row.chat_id)
    materialize_native_history(project, root, spawn_id)


def test_discovery_selects_only_the_owners_active_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = _runtime(tmp_path)
    primary = _spawn(root, "p-primary", owner=None, kind="primary")
    legacy_child = _spawn(root, "p-legacy", owner=None)
    continued_child = _spawn(root, "p-continued", chat="c-continued")
    unrelated = _spawn(root, "p-unrelated", chat="c-other", owner=None)
    history_index.HistoryIndex(root).rebuild()
    original = SpawnRecord.model_validate_json

    def reject_unrelated_candidate(raw: str | bytes) -> SpawnRecord:
        assert json.loads(raw)["id"] != unrelated, "decoded another owner's candidate"
        return original(raw)

    monkeypatch.setattr(SpawnRecord, "model_validate_json", reject_unrelated_candidate)

    rows = spawn_api._discover_pending_spawns(root, "c-owner")

    assert {row.id for row in rows} == {primary, legacy_child, continued_child}


def test_nested_discovery_reaches_live_child_through_archived_ancestor(
    tmp_path: Path,
) -> None:
    project, root = _runtime(tmp_path)
    start_row(root, "p1", HarnessId.PI, None)
    start_row(root, "p2", HarnessId.CODEX, "p1")
    start_row(root, "p-sibling", HarnessId.PI, None)
    for spawn_id in ("p1", "p2"):
        spawn_store.finalize_spawn(root, spawn_id, "succeeded", 0, origin="runner")
    _bind_pi_transcript(project, root, "p2")
    archived = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=("p2",),
        apply=True,
    )
    assert archived.reclaimed
    assert spawn_store.get_spawn(root, "p2") is None
    start_row(root, "p3", HarnessId.CODEX, "p2")
    spawn_store.mark_finalizing(root, "p3")

    rows = spawn_api._discover_pending_spawns(
        root,
        "c-owner",
        exclude_spawn_id="p1",
        only_descendants_of="p1",
    )

    assert [row.id for row in rows] == ["p3"]
    assert "p-sibling" not in {row.id for row in rows}


def test_nested_discovery_follows_reopened_runs_exact_parent_history(tmp_path: Path) -> None:
    _, root = _runtime(tmp_path)
    seed_run(root, "p1", status=None, chat_id="c1")
    original_child = seed_run(root, "p2", status=None, parent="p1", chat_id="c2")
    seed_run(root, "p9", status=None, chat_id="c9")
    reopened_child = seed_run(root, "p2.2", status=None, parent="p9", chat_id="c2.2")
    grandchild = seed_run(root, "p10", status=None, parent="p2.2", chat_id="c10")

    under_original = spawn_api._discover_pending_spawns(
        root,
        "c1",
        only_descendants_of="p1",
    )
    under_reopen = spawn_api._discover_pending_spawns(
        root,
        "c9",
        only_descendants_of="p9",
    )

    assert [row.id for row in under_original] == [original_child]
    assert {row.id for row in under_reopen} == {reopened_child, grandchild}


def test_discovery_rereads_terminal_authority_for_active_index_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = _runtime(tmp_path)
    spawn_id = _spawn(root, "p-stale")
    stale_active_row = spawn_store.get_spawn(root, spawn_id)
    assert stale_active_row is not None
    spawn_store.finalize_spawn(root, spawn_id, "succeeded", 0, origin="runner")

    # Model a terminal transition immediately after the index selected its row.
    # Discovery must not trust that provisional lifecycle snapshot over state.json.
    monkeypatch.setattr(
        history_index.HistoryIndex,
        "spawns",
        lambda self, **kwargs: (stale_active_row,),
    )

    rows = spawn_api._discover_pending_spawns(root, "c-owner")

    assert rows == []


def test_discovery_does_not_use_the_unfiltered_spawn_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = _runtime(tmp_path)
    spawn_id = _spawn(root, "p-child")
    history_index.HistoryIndex(root).rebuild()

    def reject_unfiltered_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-arg wait discovery loaded the global spawn scan")

    monkeypatch.setattr(spawn_api, "indexed_spawn_scan", reject_unfiltered_scan)

    rows = spawn_api._discover_pending_spawns(root, "c-owner")

    assert [row.id for row in rows] == [spawn_id]
