"""SQL-scoped no-argument spawn-wait discovery."""

import json
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.state.history_index as history_index
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.ops.session_archive import archive_history, materialize_native_history
from meridian.lib.ops.spawn.models import SpawnWaitInput
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.spawn.model import LaunchMode, SpawnKind, SpawnRecord
from tests.support.fakes import FakeClock
from tests.support.resident_drain import start_row


@pytest.fixture(autouse=True)
def frozen_history_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Freeze the clock history_index checks deadlines against.

    The wait deadline then never expires on a loaded host; these tests assert on discovery
    results, not on how fast they arrive.
    """
    clock = FakeClock(1000.0)
    monkeypatch.setattr(history_index, "time", clock)
    return clock


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
    chat: str = "c9008",
    owner: str | None = "c9008",
    parent: str | None = None,
    kind: SpawnKind = "child",
    launch_mode: LaunchMode | None = None,
) -> str:
    return str(
        spawn_store.start_spawn(
            root,
            spawn_id=name,
            chat_id=chat,
            owner_chat_id=owner,
            parent_id=parent,
            model="test",
            agent="coder",
            harness="pi",
            kind=kind,
            prompt="wait discovery",
            launch_mode=launch_mode,
        ).spawn_id
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


def test_wait_target_discovery_adds_only_owed_finished_background_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_history_clock: FakeClock,
) -> None:
    _, root = _runtime(tmp_path)
    primary = _spawn(root, "p-primary", owner=None, kind="primary")
    legacy_child = _spawn(root, "p-legacy", owner=None, parent=primary)
    continued_child = _spawn(root, "p-continued", chat="c-continued", parent=primary)
    unrelated = _spawn(root, "p-unrelated", chat="c-other", owner=None)
    # An earlier primary generation of this chat: its background subtree is owed to us.
    earlier = _spawn(root, "p-earlier", owner=None, kind="primary")
    earlier_child = _spawn(root, "p-earlier-child", parent=earlier, launch_mode="background")
    earlier_grandchild = _spawn(root, "p-earlier-grandchild", parent=earlier_child)
    spawn_store.finalize_spawn(root, earlier, "failed", 1, origin="runner")
    peer = _spawn(root, "p-peer")  # shares the chat, not under any owned root
    owed_ids = tuple(
        _spawn(root, f"p-owed-{index:02d}", parent=primary, launch_mode="background")
        for index in range(36)
    )
    foreground = _spawn(root, "p-foreground", parent=primary, launch_mode="foreground")
    for index, spawn_id in enumerate(owed_ids):
        spawn_store.finalize_spawn(
            root,
            spawn_id,
            "succeeded",
            0,
            origin="runner",
            finished_at=f"2026-01-01T00:00:{index:02d}Z",
        )
    spawn_store.finalize_spawn(root, foreground, "succeeded", 0, origin="runner")
    coordination_dir = root / "pi-bash" / primary
    coordination_dir.mkdir(parents=True)
    (coordination_dir / "delivery-receipts.json").write_text(
        json.dumps(
            {
                "v": 1,
                "spawn_id": primary,
                "messages": {"delivery-1": [owed_ids[0]]},
            }
        ),
        encoding="utf-8",
    )
    (coordination_dir / "cleared-spawns.json").write_text(
        json.dumps(
            {
                "v": 1,
                "spawn_id": primary,
                "updated_at_ms": 1.0,
                "cleared_spawn_ids": [owed_ids[1]],
            }
        ),
        encoding="utf-8",
    )
    history_index.HistoryIndex(root).rebuild()
    original = SpawnRecord.model_validate_json

    def reject_unrelated_candidate(raw: str | bytes) -> SpawnRecord:
        assert json.loads(raw)["id"] != unrelated, "decoded another owner's candidate"
        return original(raw)

    monkeypatch.setattr(SpawnRecord, "model_validate_json", reject_unrelated_candidate)

    targets = spawn_api._resolve_wait_targets(
        SpawnWaitInput(),
        root,
        RuntimeContext(spawn_id=SpawnId(primary), chat_id="c9008"),
        deadline=frozen_history_clock.monotonic() + 5,
    )

    assert not targets.discovery_pending
    assert targets.blocked_on_ids == (
        continued_child,
        earlier_child,
        earlier_grandchild,
        legacy_child,
    )
    assert peer not in targets.blocked_on_ids
    assert tuple(row.id for row in targets.owed_rows) == owed_ids[2:34]
    assert set(owed_ids[:2]).isdisjoint(row.id for row in targets.owed_rows)
    assert set(owed_ids[34:]).isdisjoint(row.id for row in targets.owed_rows)
    assert foreground not in targets.blocked_on_ids


def test_nested_discovery_reaches_live_child_through_archived_ancestor(
    tmp_path: Path,
    frozen_history_clock: FakeClock,
) -> None:
    project, root = _runtime(tmp_path)
    start_row(root, "p1", HarnessId.PI, None)
    start_row(root, "p2", HarnessId.CODEX, "p1")
    start_row(root, "p-sibling", HarnessId.PI, None)
    spawn_store.update_spawn(root, "p2", launch_mode="background")
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
    owed = _spawn(
        root,
        "p-direct-owed",
        chat="c-direct",
        parent="p1",
        launch_mode="background",
    )
    foreground = _spawn(
        root,
        "p-direct-foreground",
        chat="c-direct-foreground",
        parent="p1",
        launch_mode="foreground",
    )
    spawn_store.finalize_spawn(root, owed, "succeeded", 0, origin="runner")
    spawn_store.finalize_spawn(root, foreground, "succeeded", 0, origin="runner")

    targets = spawn_api._resolve_wait_targets(
        SpawnWaitInput(),
        root,
        RuntimeContext(spawn_id=SpawnId("p1"), chat_id="c9008"),
        deadline=frozen_history_clock.monotonic() + 5,
    )

    assert targets.blocked_on_ids == ("p3",)
    assert tuple(row.id for row in targets.owed_rows) == (owed,)
    assert "p-sibling" not in targets.blocked_on_ids
    assert foreground not in targets.blocked_on_ids


def test_discovery_rereads_terminal_authority_for_active_index_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = _runtime(tmp_path)
    spawn_id = _spawn(root, "p-stale")
    spawn_store.finalize_spawn(root, spawn_id, "succeeded", 0, origin="runner")

    # Model a terminal transition immediately after the index selected its row.
    # Discovery must not trust that provisional selection over state.json.
    monkeypatch.setattr(
        history_index.HistoryIndex,
        "descendant_projection",
        lambda self, roots, **kwargs: ((spawn_id, "p-root", False),),
    )

    assert spawn_api._discover_pending_spawns(root, ("p-root",)) == []
