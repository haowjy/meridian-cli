# qa-validated: test-suite-redesign

"""File-backed spawn row publication and concurrency regressions."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.ops.pruning import (
    StaleSpawnArtifact,
    prune_stale_spawn_artifacts,
    scan_stale_spawn_artifacts,
)
from meridian.lib.platform.locking import lock_file, try_lock_file
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


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(
    runtime_root: Path, *, spawn_id: str, status: SpawnStatus = "running"
) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            spawn_id=spawn_id,
            status=status,
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
    ) -> StoredSpawnState:
        state_serialization_reached.set()
        if not release_state_serialization.wait(timeout=5):
            raise TimeoutError("initial state serialization was not released")
        return original_record_to_stored_state(record)

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


def test_pruning_does_not_split_spawn_lock_identity(tmp_path: Path) -> None:
    """A pruned artifact must not let a second writer lock a new inode."""
    # Pre-fix failure: ``assert [True] == [False]`` — the contender acquired
    # the recreated path while this thread still held the unlinked old inode.
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    artifact_path = paths.spawns_dir / spawn_id
    artifact_path.mkdir(parents=True)
    lock_path = spawn_repository.spawn_lock_path(paths.spawns_dir, spawn_id)
    second_writer_acquired: list[bool] = []

    stale = [
        StaleSpawnArtifact(
            spawn_id=spawn_id,
            project_uuid="project",
            path=str(artifact_path),
            size_bytes=0,
            last_activity="2026-01-01T00:00:00+00:00",
        )
    ]

    with lock_file(lock_path):
        assert prune_stale_spawn_artifacts(stale) == 1

        def try_second_writer() -> None:
            with try_lock_file(lock_path) as handle:
                second_writer_acquired.append(handle is not None)

        contender = threading.Thread(target=try_second_writer)
        contender.start()
        contender.join(timeout=5)

    assert not contender.is_alive()
    assert second_writer_acquired == [False]


def test_pruning_preserves_spawn_with_pending_cleanup_claim(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = _start_test_spawn(runtime_root, spawn_id="p7", status="succeeded")
    claim_path = paths.spawns_dir / spawn_id / "reaper_cleanup_claim.json"
    claim_path.write_text('{"v":1,"scopes":[]}\n', encoding="utf-8")
    stale = [
        StaleSpawnArtifact(
            spawn_id=spawn_id,
            project_uuid="project",
            path=str(paths.spawns_dir / spawn_id),
            size_bytes=0,
            last_activity="2026-01-01T00:00:00+00:00",
        )
    ]

    assert prune_stale_spawn_artifacts(stale) == 0
    assert claim_path.is_file()


@pytest.mark.parametrize(
    "invalid_spawn_id",
    ["../escaped", "/absolute", ".staging", "..", "nested/child"],
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


def test_start_spawn_accepts_path_safe_symbolic_id(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)

    spawn_id = _start_test_spawn(runtime_root, spawn_id="dry-run")

    assert spawn_id == "dry-run"
    assert (paths.spawns_dir / spawn_id / "state.json").is_file()


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


def test_remove_spawn_events_cannot_leave_writer_ghost_row(tmp_path: Path) -> None:
    """Rollback deletion must coordinate with an in-flight spawn writer."""
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = str(_start_test_spawn(runtime_root, spawn_id="p7", status="queued"))
    writer_holds_lock = threading.Event()
    allow_writer = threading.Event()

    def pause_writer(record: SpawnRecord) -> SpawnRecord:
        writer_holds_lock.set()
        assert allow_writer.wait(timeout=5)
        return record.model_copy(update={"desc": "writer completed"})

    writer = threading.Thread(
        target=spawn_repository.write_state_locked,
        args=(paths.spawns_dir, spawn_id, pause_writer),
    )
    writer.start()
    assert writer_holds_lock.wait(timeout=5)

    deletion = threading.Thread(target=remove_spawn_events, args=(runtime_root, spawn_id))
    deletion.start()
    deletion.join(timeout=0.2)
    allow_writer.set()
    writer.join(timeout=5)
    deletion.join(timeout=5)

    assert not writer.is_alive()
    assert not deletion.is_alive()
    assert not (paths.spawns_dir / spawn_id).exists()
