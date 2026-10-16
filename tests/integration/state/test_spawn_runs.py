"""Immutable run-row discovery, indexing, and retention contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from meridian.lib.config.history_models import HistoryArchiveConfig
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.types import SpawnId
from meridian.lib.ops import session_archive
from meridian.lib.ops.pruning import prune_stale_spawn_artifacts, scan_stale_spawn_artifacts
from meridian.lib.ops.session_archive import archive_history
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.history_index import HistoryIndex
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.session_identity import session_records_for_spawns
from meridian.lib.state.spawn import repository as spawn_repository
from meridian.lib.state.spawn import runs as spawn_runs
from meridian.lib.state.spawn.runs import latest_run, next_run_id, runs_of
from tests.support.io_probe import IOProbe
from tests.support.native_snapshot import seal_spawn_snapshot
from tests.support.spawn_runs import reservation, seed_run


def test_run_discovery_uses_max_suffix_with_holes_and_ignores_junk(tmp_path: Path) -> None:
    seed_run(tmp_path)
    seed_run(tmp_path, "p1.2")
    seed_run(tmp_path, "p1.3")
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


def test_latest_run_rebuilds_a_missing_head_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_run(tmp_path, status=None)
    (tmp_path / "spawn-heads" / "p1").unlink()
    probe = IOProbe(monkeypatch)
    probe.watch("scan_spawn_ids", spawn_repository.scan_spawn_ids)

    assert not (tmp_path / "spawn-heads" / "p1").exists()
    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert probe.count("scan_spawn_ids") == 1
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "1\n"


def test_latest_run_caches_a_lost_head_with_no_loose_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = seed_run(tmp_path, None, status=None)
    assert spawn_store.delete_published_spawn(
        tmp_path, run_id, can_delete=lambda _row: True,
    )
    (tmp_path / "spawn-heads" / "p1").unlink()
    probe = IOProbe(monkeypatch)
    probe.watch("scan_spawn_ids", spawn_repository.scan_spawn_ids)

    assert latest_run(tmp_path, SpawnId("p1")) is None
    assert latest_run(tmp_path, SpawnId("p1")) is None
    assert probe.count("scan_spawn_ids") == 1
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "0\n"


def test_latest_run_above_spawn_counter_does_not_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert seed_run(tmp_path, None, status=None) == "p1"
    probe = IOProbe(monkeypatch)
    probe.watch("scan_spawn_ids", spawn_repository.scan_spawn_ids)

    assert latest_run(tmp_path, SpawnId("p2")) is None
    assert latest_run(tmp_path, SpawnId("p2")) is None
    assert probe.count("scan_spawn_ids") == 0
    assert not (tmp_path / "spawn-heads" / "p2").exists()


def test_latest_run_probes_above_stale_head(tmp_path: Path) -> None:
    seed_run(tmp_path, status=None)
    seed_run(tmp_path, "p1.2", status=None)
    seed_run(tmp_path, "p1.3", status=None)
    (tmp_path / "spawn-heads" / "p1").write_text("2\n", encoding="utf-8")

    assert latest_run(tmp_path, SpawnId("p1")) == "p1.3"


def test_latest_run_probes_below_head_written_before_publish(tmp_path: Path) -> None:
    seed_run(tmp_path, status=None)
    (tmp_path / "spawn-heads" / "p1").write_text("3\n", encoding="utf-8")

    assert latest_run(tmp_path, SpawnId("p1")) == "p1"


def test_latest_run_stats_only_target_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_run(tmp_path, status=None)
    for number in range(2, 12):
        state = tmp_path / "spawns" / f"p{number}" / "state.json"
        state.parent.mkdir()
        state.write_text("{}\n", encoding="utf-8")

    probe = IOProbe(monkeypatch)
    probe.watch("run_exists", spawn_runs._run_exists)

    assert latest_run(tmp_path, SpawnId("p1")) == "p1"
    assert 1 <= probe.count("run_exists") <= 2
    assert all(call.args[1] == SpawnId("p1") for call in probe.calls("run_exists"))


def test_spawn_counter_ignores_dotted_run_suffixes(tmp_path: Path) -> None:
    seed_run(tmp_path, "p100")
    seed_run(tmp_path, "p100.2")
    spawn_store.delete_published_spawn(
        tmp_path,
        parse_ref("p100", row_key=True),
        can_delete=lambda _row: True,
    )
    (tmp_path / "spawn-id-counter").unlink(missing_ok=True)

    assert seed_run(tmp_path, None, status=None) == "p101"


def test_history_index_traverses_multi_run_spawn_chain_without_duplicates(
    tmp_path: Path,
) -> None:
    seed_run(tmp_path, "p1")
    seed_run(tmp_path, "p2", parent="p1")
    seed_run(tmp_path, "p2.2", parent="p1")
    seed_run(tmp_path, "p3", parent="p2")
    seed_run(tmp_path, "p3.2", parent="p2")
    seed_run(tmp_path, "p4", parent="p3")
    seed_run(tmp_path, "p4.2", parent="p3")

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


def test_history_index_projects_only_runs_parented_within_each_subtree(
    tmp_path: Path,
) -> None:
    seed_run(tmp_path, "p1")
    seed_run(tmp_path, "p2", parent="p1")
    seed_run(tmp_path, "p9")
    reopened_child = spawn_store.reserve_run(
        tmp_path,
        SpawnId("p2"),
        reservation(
            parent_id="p9",
            parent_run_id=parse_ref("p9", row_key=True),
            agent="coder",
        ),
    )
    grandchild = seed_run(tmp_path, "p10", status=None, parent=str(reopened_child))

    index = HistoryIndex(tmp_path)
    assert index.rebuild().complete
    assert index.descendant_projection("p1") == (("p2", "p1", False),)
    assert index.descendant_projection("p1", active_only=True) == ()
    assert index.descendant_projection("p9") == (
        ("p10", "p2", False),
        ("p2.2", "p9", False),
    )
    assert index.descendant_projection("p9", active_only=True) == (
        (str(grandchild), "p2", False),
        (str(reopened_child), "p9", False),
    )


def test_bound_generations_and_clear_segment_survive_multi_run_archive_discovery(
    tmp_path: Path,
) -> None:
    first = seed_run(tmp_path, "p1", bound=True)

    second = seed_run(tmp_path, "p1.2", bound=True)
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
    seed_run(tmp_path, "p1", bound=True)
    seed_run(tmp_path, "p1.2")
    third = seed_run(tmp_path, "p1.3", status=None)
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
    seed_run(tmp_path, "p1", bound=True)
    seed_run(tmp_path, "p1.2")

    stale = scan_stale_spawn_artifacts(
        tmp_path,
        retention_days=0,
        active_spawn_ids=set(),
        now=10**10,
    )
    assert [row.spawn_id for row in stale] == ["p1", "p1.2"]
    assert prune_stale_spawn_artifacts(stale).removed == 2
    probe = IOProbe(monkeypatch)
    probe.watch("scan_spawn_ids", spawn_repository.scan_spawn_ids)

    assert runs_of(tmp_path, SpawnId("p1")) == ()
    assert runs_of(tmp_path, SpawnId("p1")) == ()
    assert probe.count("scan_spawn_ids") == 0
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "2\n"


def test_retired_head_prevents_reusing_partially_restored_run_keys(tmp_path: Path) -> None:
    for key in ("p1", "p1.2", "p1.3"):
        seed_run(tmp_path, key)
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
    seed_run(tmp_path, "p1", bound=True)
    seed_run(tmp_path, "p1.2", bound=True)
    seed_run(tmp_path, "p1.3")
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
    root = tmp_path / "runtime"
    for key in ("p1", "p1.2", "p1.3"):
        if key != "p1.3":
            seed_run(root, key, bound=True)
        else:
            seed_run(root, key)
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
    first = seed_run(tmp_path, "p1", bound=True)
    stale = scan_stale_spawn_artifacts(
        tmp_path, retention_days=0, active_spawn_ids=set(), now=10**10,
    )
    spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation())
    assert prune_stale_spawn_artifacts(stale).removed == 0
    assert spawn_store.get_spawn(tmp_path, first) is not None
