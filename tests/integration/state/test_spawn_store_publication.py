# qa-validated: test-suite-redesign

"""File-backed spawn row publication and concurrency regressions."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from meridian.lib.ops import pruning as pruning_module
from meridian.lib.ops.pruning import (
    prune_orphan_project_dirs,
    prune_stale_spawn_artifacts,
    scan_orphan_project_dirs,
    scan_stale_spawn_artifacts,
)
from meridian.lib.platform.locking import lock_file, try_lock_file
from meridian.lib.state import paths as paths_module
from meridian.lib.state import spawn_store as spawn_store_module
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn import repository as spawn_repository
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import StoredSpawnState, read_state, scan_spawn_ids
from meridian.lib.state.spawn_store import (
    gc_abandoned_stages,
    remove_spawn_events,
    start_spawn,
)
from meridian.lib.state.user_paths import get_user_home
from tests.conftest import posix_only


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(runtime_root: Path, *, spawn_id: str) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            spawn_id=spawn_id,
        )
    )


@contextmanager
def _paused_initial_publication(
    runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawn_id: str,
) -> Generator[None, None, None]:
    state_serialization_reached = threading.Event()
    release_state_serialization = threading.Event()
    errors: list[BaseException] = []
    original_record_to_stored_state = spawn_repository.record_to_stored_state

    def pause_before_state_serialization(
        record: SpawnRecord,
        *,
        revision: int,
    ) -> StoredSpawnState:
        state_serialization_reached.set()
        if not release_state_serialization.wait(timeout=5):
            raise TimeoutError("initial state serialization was not released")
        return original_record_to_stored_state(record, revision=revision)

    monkeypatch.setattr(
        spawn_repository,
        "record_to_stored_state",
        pause_before_state_serialization,
    )

    def create_spawn() -> None:
        try:
            _start_test_spawn(runtime_root, spawn_id=spawn_id)
        except BaseException as exc:
            errors.append(exc)

    publisher = threading.Thread(target=create_spawn)
    publisher.start()
    try:
        assert state_serialization_reached.wait(timeout=5)
        yield
    finally:
        release_state_serialization.set()
        publisher.join(timeout=5)

    assert not publisher.is_alive()
    assert errors == []


def test_start_spawn_never_exposes_final_row_before_initial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"

    with _paused_initial_publication(
        runtime_root,
        monkeypatch,
        spawn_id=spawn_id,
    ):
        assert scan_spawn_ids(paths.spawns_dir) == []
        assert not (paths.spawns_dir / spawn_id).exists()
        stages = list((paths.spawns_dir / ".staging").iterdir())
        assert len(stages) == 1
        assert stages[0].name.startswith(f"{spawn_id}-")
        assert not (stages[0] / "state.json").exists()

    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.id == spawn_id


def test_gc_waits_for_in_progress_publication_and_preserves_published_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    gc_lock_contended = threading.Event()
    gc_finished = threading.Event()
    errors: list[BaseException] = []
    original_gc_lock_file = spawn_store_module.lock_file

    @contextmanager
    def observe_gc_lock(path: Path) -> Generator[object, None, None]:
        if threading.current_thread() is collector:
            with try_lock_file(path) as lock_handle:
                assert lock_handle is None
            gc_lock_contended.set()
        with original_gc_lock_file(path) as lock_handle:
            yield lock_handle

    monkeypatch.setattr(spawn_store_module, "lock_file", observe_gc_lock)

    def collect_abandoned_stages() -> None:
        try:
            gc_abandoned_stages(runtime_root)
        except BaseException as exc:
            errors.append(exc)
        finally:
            gc_finished.set()

    collector = threading.Thread(target=collect_abandoned_stages)

    try:
        with _paused_initial_publication(
            runtime_root,
            monkeypatch,
            spawn_id=spawn_id,
        ):
            collector.start()
            assert gc_lock_contended.wait(timeout=5)
            assert not gc_finished.is_set()
    finally:
        if collector.ident is not None:
            collector.join(timeout=5)

    assert not collector.is_alive()
    assert errors == []
    assert list((paths.spawns_dir / ".staging").iterdir()) == []
    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.id == spawn_id


def test_pruning_revalidates_stale_active_snapshot_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"

    with _paused_initial_publication(
        runtime_root,
        monkeypatch,
        spawn_id=spawn_id,
    ):
        active_spawn_ids = set(scan_spawn_ids(paths.spawns_dir))
        assert active_spawn_ids == set()

    stale = scan_stale_spawn_artifacts(
        runtime_root,
        retention_days=0,
        active_spawn_ids=active_spawn_ids,
        now=0.0,
    )
    assert [artifact.spawn_id for artifact in stale] == [spawn_id]
    assert prune_stale_spawn_artifacts(stale) == 0
    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.status == "running"


def test_orphan_pruning_waits_for_publication_and_revalidates_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    runtime_root = user_home / "projects" / "project-id"
    runtime_root.mkdir(parents=True)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    prune_lock_contended = threading.Event()
    prune_finished = threading.Event()
    removed: list[int] = []
    errors: list[BaseException] = []

    with _paused_initial_publication(
        runtime_root,
        monkeypatch,
        spawn_id=spawn_id,
    ):
        candidates = scan_orphan_project_dirs(
            user_home,
            retention_days=0,
            now=0.0,
        )
        assert [candidate.uuid for candidate in candidates] == ["project-id"]
        original_pruning_lock_file = pruning_module.lock_file

        @contextmanager
        def observe_pruning_lock(path: Path) -> Generator[object, None, None]:
            if threading.current_thread() is pruner:
                with try_lock_file(path) as lock_handle:
                    assert lock_handle is None
                prune_lock_contended.set()
            with original_pruning_lock_file(path) as lock_handle:
                yield lock_handle

        monkeypatch.setattr(pruning_module, "lock_file", observe_pruning_lock)

        def prune_candidates() -> None:
            try:
                removed.append(prune_orphan_project_dirs(candidates))
            except BaseException as exc:
                errors.append(exc)
            finally:
                prune_finished.set()

        pruner = threading.Thread(target=prune_candidates)
        pruner.start()
        assert prune_lock_contended.wait(timeout=5)
        assert not prune_finished.is_set()

    pruner.join(timeout=5)
    assert not pruner.is_alive()
    assert errors == []
    assert removed == [0]
    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.prompt == "hello"


def test_pruning_ignores_in_progress_publication_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    lookalike = paths.spawns_dir / "not-a-spawn"
    lookalike.mkdir(parents=True)
    lookalike_marker = lookalike / "keep.txt"
    lookalike_marker.write_text("keep\n", encoding="utf-8")

    with _paused_initial_publication(
        runtime_root,
        monkeypatch,
        spawn_id=spawn_id,
    ):
        stale = scan_stale_spawn_artifacts(
            runtime_root,
            retention_days=0,
            active_spawn_ids=set(),
            now=0.0,
        )
        assert stale == []
        assert prune_stale_spawn_artifacts(stale) == 0
        assert len(list((paths.spawns_dir / ".staging").iterdir())) == 1
        assert lookalike_marker.read_text(encoding="utf-8") == "keep\n"

    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.id == spawn_id


def test_project_pruning_keeps_one_lock_identity_for_waiting_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    runtime_root = user_home / "projects" / "project-id"
    runtime_root.mkdir(parents=True)
    (runtime_root / "stale.txt").write_text("stale\n", encoding="utf-8")
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    prune_revalidation_reached = threading.Event()
    release_prune_revalidation = threading.Event()
    publisher_lock_contended = threading.Event()
    state_serialization_reached = threading.Event()
    release_state_serialization = threading.Event()
    errors: list[BaseException] = []
    removed: list[int] = []
    original_spawn_lock_file = spawn_store_module.lock_file
    original_record_to_stored_state = spawn_repository.record_to_stored_state

    @contextmanager
    def observe_publisher_lock(path: Path) -> Generator[object, None, None]:
        if threading.current_thread() is publisher:
            with try_lock_file(path) as lock_handle:
                assert lock_handle is None
            publisher_lock_contended.set()
        with original_spawn_lock_file(path) as lock_handle:
            yield lock_handle

    def pause_before_state_serialization(
        record: SpawnRecord,
        *,
        revision: int,
    ) -> StoredSpawnState:
        state_serialization_reached.set()
        if not release_state_serialization.wait(timeout=5):
            raise TimeoutError("state serialization was not released")
        return original_record_to_stored_state(record, revision=revision)

    monkeypatch.setattr(spawn_store_module, "lock_file", observe_publisher_lock)
    monkeypatch.setattr(
        spawn_repository,
        "record_to_stored_state",
        pause_before_state_serialization,
    )
    candidates = scan_orphan_project_dirs(user_home, retention_days=0, now=0.0)
    original_list_spawns = pruning_module.spawn_store.list_spawns

    def pause_project_revalidation(root: Path) -> list[SpawnRecord]:
        if threading.current_thread() is pruner:
            prune_revalidation_reached.set()
            if not release_prune_revalidation.wait(timeout=5):
                raise TimeoutError("project revalidation was not released")
        return original_list_spawns(root)

    monkeypatch.setattr(
        pruning_module.spawn_store,
        "list_spawns",
        pause_project_revalidation,
    )

    def prune_project() -> None:
        try:
            removed.append(prune_orphan_project_dirs(candidates))
        except BaseException as exc:
            errors.append(exc)

    def publish_spawn() -> None:
        try:
            _start_test_spawn(runtime_root, spawn_id=spawn_id)
        except BaseException as exc:
            errors.append(exc)

    pruner = threading.Thread(target=prune_project)
    publisher = threading.Thread(target=publish_spawn)
    pruner.start()
    assert prune_revalidation_reached.wait(timeout=5)
    publisher.start()
    assert publisher_lock_contended.wait(timeout=5)
    release_prune_revalidation.set()
    assert state_serialization_reached.wait(timeout=5)

    try:
        with try_lock_file(paths.spawns_flock) as lock_handle:
            assert lock_handle is None
    finally:
        release_state_serialization.set()
        pruner.join(timeout=5)
        publisher.join(timeout=5)

    assert not pruner.is_alive()
    assert not publisher.is_alive()
    assert errors == []
    assert removed == [1]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.prompt == "hello"


@posix_only
def test_spawn_lock_identity_is_shared_by_runtime_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real-parent" / "runtime"
    real_root.mkdir(parents=True)
    alias_root = tmp_path / "alias-parent" / "runtime-alias"
    alias_root.parent.mkdir()
    alias_root.symlink_to(real_root, target_is_directory=True)

    real_lock = RuntimePaths.from_root_dir(real_root).spawns_flock
    alias_lock = RuntimePaths.from_root_dir(alias_root).spawns_flock

    assert real_lock == alias_lock
    alias_blocked: list[bool] = []

    def try_alias_lock() -> None:
        with try_lock_file(alias_lock) as alias_handle:
            alias_blocked.append(alias_handle is None)

    contender = threading.Thread(target=try_alias_lock)
    with lock_file(real_lock):
        contender.start()
        contender.join(timeout=5)

    assert not contender.is_alive()
    assert alias_blocked == [True]


def test_spawn_lock_uses_fixed_length_managed_namespace(tmp_path: Path) -> None:
    long_runtime_root = tmp_path / ("r" * 250)

    lock_path = RuntimePaths.from_root_dir(long_runtime_root).spawns_flock

    assert lock_path.parent == get_user_home() / "locks" / "spawns"
    assert len(lock_path.name) == 70


def test_spawn_lock_unifies_macos_case_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths_module.sys, "platform", "darwin")

    upper_lock = RuntimePaths.from_root_dir(tmp_path / "Runtime").spawns_flock
    lower_lock = RuntimePaths.from_root_dir(tmp_path / "runtime").spawns_flock

    assert upper_lock == lower_lock


@pytest.mark.parametrize(
    "invalid_spawn_id",
    ["../escaped", "/absolute", ".staging", "p\u0661", "p"],
)
def test_start_spawn_rejects_explicit_id_outside_spawn_namespace(
    tmp_path: Path,
    invalid_spawn_id: str,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)

    with pytest.raises(ValueError, match="Invalid spawn ID"):
        _start_test_spawn(runtime_root, spawn_id=invalid_spawn_id)

    assert scan_spawn_ids(paths.spawns_dir) == []
    assert not (runtime_root / "escaped").exists()
    assert not (paths.spawns_dir / ".staging").exists()


def test_start_spawn_explicit_id_collision_preserves_existing_row(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    _start_test_spawn(runtime_root, spawn_id=spawn_id)
    spawn_dir = paths.spawns_dir / spawn_id
    before = {
        path.relative_to(spawn_dir).as_posix(): path.read_bytes()
        for path in spawn_dir.rglob("*")
        if path.is_file()
    }

    with pytest.raises(FileExistsError, match="Refusing to publish over existing destination"):
        start_spawn(
            runtime_root,
            chat_id="collision",
            model="different-model",
            agent="different-agent",
            harness="pi",
            prompt="replacement",
            spawn_id=spawn_id,
        )

    after = {
        path.relative_to(spawn_dir).as_posix(): path.read_bytes()
        for path in spawn_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.chat_id == "c1"
    assert row.prompt == "hello"


def test_remove_spawn_events_rejects_staging_container(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    staged_row = paths.spawns_dir / ".staging" / "p7-123-deadbeef"
    staged_row.mkdir(parents=True)
    staged_state = staged_row / "state.json"
    staged_state.write_text("in progress\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid spawn ID"):
        remove_spawn_events(runtime_root, ".staging")

    assert staged_state.read_text(encoding="utf-8") == "in progress\n"
