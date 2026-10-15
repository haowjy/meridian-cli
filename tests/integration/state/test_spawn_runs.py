"""Immutable run-row discovery, indexing, and retention contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.ops.pruning import prune_stale_spawn_artifacts, scan_stale_spawn_artifacts
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.session_identity import session_records_for_spawns
from meridian.lib.state.spawn import repository as spawn_repository
from meridian.lib.state.spawn.runs import latest_run, next_run_id, runs_of
from tests.support.native_snapshot import seal_spawn_snapshot


def _start(
    root: Path,
    *,
    spawn_id: str | None = "p1",
    parent_id: SpawnId | None = None,
) -> RunId:
    return spawn_store.start_spawn(
        root,
        spawn_id=spawn_id,
        chat_id="c1",
        model="test",
        agent="coder",
        harness="codex",
        prompt="hello",
        parent_id=parent_id,
    )


def _finish(root: Path, run_id: object) -> None:
    # These fixtures represent a completed launch whose runner has exited.
    spawn_store.update_spawn(root, parse_ref(str(run_id), row_key=True), runner_pid=999_999_999)
    spawn_store.finalize_spawn(
        root,
        parse_ref(str(run_id), row_key=True),
        status="succeeded",
        exit_code=0,
        origin="runner",
    )


def _bound(root: Path, key: str) -> RunId:
    run_id = _start(root, spawn_id=key)
    session_store.start_session(
        root, harness="codex", harness_session_id="native-1", model="test",
        chat_id="c1", spawn_id=run_id,
    )
    session_store.stop_session(root, "c1")
    _finish(root, run_id)
    return run_id


def test_run_discovery_uses_max_suffix_with_holes_and_ignores_junk(tmp_path: Path) -> None:
    first = _start(tmp_path)
    _finish(tmp_path, first)
    second = _start(tmp_path, spawn_id="p1.2")
    _finish(tmp_path, second)
    third = _start(tmp_path, spawn_id="p1.3")
    _finish(tmp_path, third)
    spawn_store.delete_published_spawn(
        tmp_path,
        parse_ref("p1.2", row_key=True),
        can_delete=lambda _row: True,
    )
    junk = tmp_path / "spawns" / "r99"
    junk.mkdir()
    (junk / "state.json").write_text("{}\n", encoding="utf-8")

    assert runs_of(tmp_path, SpawnId("p1")) == ("p1", "p1.3")
    assert latest_run(tmp_path, SpawnId("p1")) == "p1.3"


def test_latest_run_rebuilds_a_missing_pointer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start(tmp_path)
    (tmp_path / "spawn-heads" / "p1").unlink()
    scans = 0
    scan = spawn_repository.scan_spawn_ids

    def count_scan(spawns_dir: Path) -> list[RunId]:
        nonlocal scans
        scans += 1
        return scan(spawns_dir)

    monkeypatch.setattr(spawn_repository, "scan_spawn_ids", count_scan)

    assert not (tmp_path / "spawn-heads" / "p1").exists()
    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert scans == 1
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "1\n"


def test_latest_run_caches_a_lost_head_with_no_loose_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _start(tmp_path, spawn_id=None)
    assert spawn_store.delete_published_spawn(
        tmp_path, run_id, can_delete=lambda _row: True,
    )
    (tmp_path / "spawn-heads" / "p1").unlink()
    scans = 0
    scan = spawn_repository.scan_spawn_ids

    def count_scan(spawns_dir: Path) -> list[RunId]:
        nonlocal scans
        scans += 1
        return scan(spawns_dir)

    monkeypatch.setattr(spawn_repository, "scan_spawn_ids", count_scan)

    assert latest_run(tmp_path, SpawnId("p1")) is None
    assert latest_run(tmp_path, SpawnId("p1")) is None
    assert scans == 1
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "0\n"


def test_latest_run_above_spawn_counter_does_not_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _start(tmp_path, spawn_id=None) == "p1"
    scans = 0

    def count_scan(_spawns_dir: Path) -> list[RunId]:
        nonlocal scans
        scans += 1
        return []

    monkeypatch.setattr(spawn_repository, "scan_spawn_ids", count_scan)

    assert latest_run(tmp_path, SpawnId("p2")) is None
    assert latest_run(tmp_path, SpawnId("p2")) is None
    assert scans == 0
    assert not (tmp_path / "spawn-heads" / "p2").exists()


def test_latest_run_probes_above_stale_pointer(tmp_path: Path) -> None:
    _start(tmp_path)
    _start(tmp_path, spawn_id="p1.2")
    _start(tmp_path, spawn_id="p1.3")
    (tmp_path / "spawn-heads" / "p1").write_text("2\n", encoding="utf-8")

    assert latest_run(tmp_path, SpawnId("p1")) == "p1.3"


def test_latest_run_probes_below_pointer_written_before_publish(tmp_path: Path) -> None:
    _start(tmp_path)
    (tmp_path / "spawn-heads" / "p1").write_text("3\n", encoding="utf-8")

    assert latest_run(tmp_path, SpawnId("p1")) == "p1"


@pytest.mark.parametrize("unrelated_count", [10, 200])
def test_latest_run_stats_only_target_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unrelated_count: int,
) -> None:
    _start(tmp_path)
    for number in range(2, unrelated_count + 2):
        state = tmp_path / "spawns" / f"p{number}" / "state.json"
        state.parent.mkdir()
        state.write_text("{}\n", encoding="utf-8")

    original_is_file = Path.is_file
    checked_state_paths: list[Path] = []

    def count_is_file(path: Path) -> bool:
        if path.name == "state.json":
            checked_state_paths.append(path)
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", count_is_file)

    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert checked_state_paths == [
        tmp_path / "spawns" / "p1.2" / "state.json",
        tmp_path / "spawns" / "p1" / "state.json",
    ]


def test_spawn_counter_ignores_dotted_run_suffixes(tmp_path: Path) -> None:
    first = _start(tmp_path, spawn_id="p100")
    _finish(tmp_path, first)
    second = _start(tmp_path, spawn_id="p100.2")
    _finish(tmp_path, second)
    spawn_store.delete_published_spawn(
        tmp_path,
        parse_ref("p100", row_key=True),
        can_delete=lambda _row: True,
    )
    (tmp_path / "spawn-id-counter").unlink(missing_ok=True)

    assert _start(tmp_path, spawn_id=None) == "p101"


def test_history_index_traverses_multi_run_spawn_chain_without_duplicates(
    tmp_path: Path,
) -> None:
    parent = _start(tmp_path, spawn_id="p1")
    _finish(tmp_path, parent)
    child = _start(tmp_path, spawn_id="p2", parent_id=SpawnId("p1"))
    _finish(tmp_path, child)
    child_second = _start(tmp_path, spawn_id="p2.2", parent_id=SpawnId("p1"))
    _finish(tmp_path, child_second)
    grandchild = _start(tmp_path, spawn_id="p3", parent_id=SpawnId("p2"))
    _finish(tmp_path, grandchild)
    grandchild_second = _start(tmp_path, spawn_id="p3.2", parent_id=SpawnId("p2"))
    _finish(tmp_path, grandchild_second)
    great_grandchild = _start(tmp_path, spawn_id="p4", parent_id=SpawnId("p3"))
    _finish(tmp_path, great_grandchild)
    great_grandchild_second = _start(tmp_path, spawn_id="p4.2", parent_id=SpawnId("p3"))
    _finish(tmp_path, great_grandchild_second)

    index = HistoryIndex(tmp_path)
    assert index.rebuild().complete
    assert {row.id for row in index.spawns()} == {
        "p1", "p2", "p2.2", "p3", "p3.2", "p4", "p4.2",
    }
    assert index.descendant_projection("p1") == (
        ("p2", "p1", False),
        ("p2.2", "p1", False),
        ("p3", "p2", False),
        ("p3.2", "p2", False),
        ("p4", "p3", False),
        ("p4.2", "p3", False),
    )


def test_bound_generations_and_clear_segment_survive_multi_run_archive_discovery(
    tmp_path: Path,
) -> None:
    first = _bound(tmp_path, "p1")

    second = _bound(tmp_path, "p1.2")
    session_store.start_session(
        tmp_path,
        harness="codex",
        harness_session_id="native-2",
        model="test",
        chat_id="c2",
    )
    session_store.stop_session(tmp_path, "c2")
    spawn_store.update_spawn(
        tmp_path,
        parse_ref(str(second), row_key=True),
        current_chat_id="c2",
    )

    rows = spawn_store.list_spawns(tmp_path).records
    linked = session_records_for_spawns(tmp_path, rows)
    assert set(linked) == {"p1", "p1.2"}
    assert linked["p1.2"].session_instance_id != linked["p1"].session_instance_id
    latest = spawn_store.get_spawn(tmp_path, parse_ref("p1.2", row_key=True))
    assert latest is not None and latest.current_chat_id == "c2"

    seal_spawn_snapshot(tmp_path, str(first))
    seal_spawn_snapshot(tmp_path, str(second))
    output = archive_history(
        tmp_path,
        destination=tmp_path.parent / "archives",
        refs=("p1",),
        apply=False,
    )
    assert set(output.selected) == {str(row.history_id) for row in rows}
    assert not output.protected
    assert not output.errors


def test_active_spawn_protects_latest_and_latest_bound_run_from_retention(
    tmp_path: Path,
) -> None:
    _bound(tmp_path, "p1")
    second = _start(tmp_path, spawn_id="p1.2")
    _finish(tmp_path, second)
    third = _start(tmp_path, spawn_id="p1.3")
    assert third == "p1.3"

    output = archive_history(
        tmp_path,
        destination=tmp_path.parent / "archives",
        eligible=True,
        apply=False,
        after_days=0,
    )
    assert {"p1", "p1.3"} <= set(output.protected)


def test_stale_artifact_pruning_keeps_head_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bound(tmp_path, "p1")
    second = _start(tmp_path, spawn_id="p1.2")
    _finish(tmp_path, second)

    stale = scan_stale_spawn_artifacts(
        tmp_path,
        retention_days=0,
        active_spawn_ids=set(),
        now=10**10,
    )
    assert [row.spawn_id for row in stale] == ["p1", "p1.2"]
    assert prune_stale_spawn_artifacts(stale).removed == 2
    scans = 0
    scan = spawn_repository.scan_spawn_ids

    def count_scan(spawns_dir: Path) -> list[RunId]:
        nonlocal scans
        scans += 1
        return scan(spawns_dir)

    monkeypatch.setattr(spawn_repository, "scan_spawn_ids", count_scan)

    assert runs_of(tmp_path, SpawnId("p1")) == ()
    assert runs_of(tmp_path, SpawnId("p1")) == ()
    assert scans == 0
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "2\n"


def test_retired_head_prevents_reusing_partially_restored_run_keys(tmp_path: Path) -> None:
    for key in ("p1", "p1.2", "p1.3"):
        _finish(tmp_path, _start(tmp_path, spawn_id=key))
    backup = tmp_path / "archived-p1"
    shutil.copytree(tmp_path / "spawns" / "p1", backup)

    for key in ("p1", "p1.2", "p1.3"):
        assert spawn_store.delete_published_spawn(
            tmp_path, parse_ref(key, row_key=True), can_delete=lambda _row: True,
        )
    shutil.copytree(backup, tmp_path / "spawns" / "p1")

    paths = RuntimePaths.from_root_dir(tmp_path)
    with lock_file(paths.spawns_flock):
        assert next_run_id(paths, SpawnId("p1")) == "p1.4"


def test_prune_failure_preserves_latest_and_bound_runs(tmp_path: Path, monkeypatch) -> None:
    _bound(tmp_path, "p1")
    _bound(tmp_path, "p1.2")
    _finish(tmp_path, _start(tmp_path, spawn_id="p1.3"))
    delete = spawn_store.delete_published_spawn
    monkeypatch.setattr(
        spawn_store, "delete_published_spawn",
        lambda root, key, **kwargs: False if key == "p1" else delete(root, key, **kwargs),
    )
    candidates = scan_stale_spawn_artifacts(
        tmp_path, retention_days=0, active_spawn_ids=set(), now=10**10,
    )
    assert prune_stale_spawn_artifacts(candidates).removed == 0
    assert runs_of(tmp_path, SpawnId("p1")) == ("p1", "p1.2", "p1.3")


@pytest.mark.parametrize("mode", ["complete", "retire-failure", "limited"])
def test_archive_apply_preserves_continuation_source_on_partial_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from meridian.lib.config.history_models import HistoryArchiveConfig
    from meridian.lib.ops import session_archive

    root = tmp_path / "runtime"
    for key in ("p1", "p1.2", "p1.3"):
        if key != "p1.3":
            _bound(root, key)
        else:
            _finish(root, _start(root, spawn_id=key))
        seal_spawn_snapshot(root, key)
    if mode == "retire-failure":
        retire = session_archive.retire_published_spawn
        monkeypatch.setattr(
            session_archive, "retire_published_spawn",
            lambda root, key, **kwargs: None if key == "p1" else retire(root, key, **kwargs),
        )
    result = archive_history(
        root, destination=tmp_path / "archives", refs=("p1",), apply=True,
        policy=HistoryArchiveConfig(max_records=1) if mode == "limited" else None,
    )
    remaining = runs_of(root, SpawnId("p1"))
    if mode == "complete":
        assert len(result.reclaimed) == 3
        assert remaining == ()
        assert (root / "spawn-heads" / "p1").read_text(encoding="utf-8") == "3\n"
    elif mode == "limited":
        assert result.limited
        assert len(result.reclaimed) == 1
        assert remaining == ("p1.2", "p1.3")
    else:
        assert not result.reclaimed
        assert remaining == ("p1", "p1.2", "p1.3")


def test_prune_rechecks_siblings_published_after_scan(tmp_path: Path) -> None:
    first = _bound(tmp_path, "p1")
    stale = scan_stale_spawn_artifacts(
        tmp_path, retention_days=0, active_spawn_ids=set(), now=10**10,
    )
    spawn_store.reserve_run(tmp_path, SpawnId("p1"), SpawnReservation(
        chat_id="c1", prompt="again", session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    ))
    assert prune_stale_spawn_artifacts(stale).removed == 0
    assert spawn_store.get_spawn(tmp_path, first) is not None
