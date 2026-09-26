"""History initialization keeps cold builds separate from bounded warm queries."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from meridian.lib.state import history_index
from meridian.lib.state.history_index import HistoryIndex


def test_simultaneous_cold_callers_reuse_one_published_build(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)
    rendezvous = threading.Barrier(2)
    caller = threading.local()
    original_lock = history_index.lock_file

    @contextmanager
    def synchronized_lock(path, **kwargs):
        if path == index.catchup_lock and not getattr(caller, "entered", False):
            caller.entered = True
            rendezvous.wait(timeout=5)
        with original_lock(path, **kwargs) as handle:
            yield handle

    monkeypatch.setattr(history_index, "lock_file", synchronized_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        builds = list(pool.map(lambda _: index.catch_up().build, range(2)))
    assert len(set(builds)) == 1


def test_cold_query_can_exceed_the_warm_two_second_budget(tmp_path: Path, monkeypatch) -> None:
    original_project = HistoryIndex._project

    def slow_sessions(self, db, source):
        if source.kind == "sessions":
            time.sleep(2.1)
        return original_project(self, db, source)

    monkeypatch.setattr(HistoryIndex, "_project", slow_sessions)
    assert HistoryIndex(tmp_path).spawns() == ()


@pytest.mark.parametrize("invalid_state", ["private state content\n", "[]\n", "null\n"])
def test_failure_is_sticky_until_manual_rebuild(tmp_path: Path, invalid_state: str) -> None:
    from meridian.lib.state import spawn_store

    key = spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    state = tmp_path / "spawns" / key / "state.json"
    valid_state = state.read_bytes()
    state.write_text(invalid_state)
    index = HistoryIndex(tmp_path)
    with pytest.raises(history_index.HistoryIndexIncomplete, match="will not retry"):
        index.spawns()
    failure = index.failure_path.read_bytes()
    assert b"private state content" not in failure
    assert index.inspect().baseline == "failed"
    state.write_bytes(valid_state)
    spawn_store.update_spawn(tmp_path, key, work_id="after-failure")
    with pytest.raises(history_index.HistoryIndexIncomplete, match="--metadata-only"):
        index.spawns()
    assert index.failure_path.read_bytes() == failure
    assert index.rebuild().complete
    assert not index.failure_path.exists()
    assert [row.id for row in index.spawns(work_id="after-failure")] == [key]


def test_dogfood_migration_rearms_authority_failure(tmp_path: Path) -> None:
    import json

    from meridian.lib.state import spawn_store
    from meridian.lib.state.spawn.dogfood_migration import migrate_dogfood_spawn_rows

    key = spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    state = tmp_path / "spawns" / key / "state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["entry_chat_id"] = "c1"
    state.write_text(json.dumps(data), encoding="utf-8")
    index = HistoryIndex(tmp_path)

    with pytest.raises(history_index.HistoryIndexIncomplete, match="meridian doctor"):
        index.spawns()
    marker = json.loads(index.failure_path.read_text(encoding="utf-8"))
    assert marker["code"] == "authority"
    assert "meridian doctor" in marker["reason"]

    migration = migrate_dogfood_spawn_rows(tmp_path)
    assert migration.migrated == (key,)
    assert not index.failure_path.exists()
    assert [row.id for row in index.spawns()] == [key]


def test_quarantined_state_authority_failure_names_non_dogfood_row(tmp_path: Path) -> None:
    from meridian.lib.state import spawn_store

    key = spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    spawn_store.finalize_spawn(tmp_path, key, status="succeeded", exit_code=0, origin="runner")
    state = tmp_path / "spawns" / key / "state.json"
    state.write_text('{"truncated":', encoding="utf-8")
    index = HistoryIndex(tmp_path)

    with pytest.raises(history_index.HistoryIndexIncomplete) as failure:
        index.spawns()

    assert str(state) in str(failure.value)
    assert "meridian doctor" not in str(failure.value)


def test_owned_timeout_is_sticky_but_cancellation_is_not(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)
    original_project = HistoryIndex._project

    def slow_project(self, db, source):
        time.sleep(0.06)
        return original_project(self, db, source)

    with monkeypatch.context() as patch:
        patch.setattr(HistoryIndex, "_project", slow_project)
        with pytest.raises(history_index.HistoryIndexIncomplete, match="remaining initialization"):
            index.initialize(deadline=time.monotonic() + 0.04)
    assert index.failure_path.exists()
    index.rebuild()
    index.path.unlink()

    def cancel(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(HistoryIndex, "_project", cancel)
    with pytest.raises(KeyboardInterrupt):
        index.spawns()
    assert not index.failure_path.exists()
    assert not index.stage.exists()


def test_source_contention_is_not_a_sticky_failure(tmp_path: Path) -> None:
    from meridian.lib.platform.locking import FileLockTimeout, lock_file
    from meridian.lib.state.history_changes import HistorySource

    index = HistoryIndex(tmp_path)
    held, release = threading.Event(), threading.Event()

    def own_source():
        with lock_file(HistorySource(kind="sessions").lock_path(tmp_path)):
            held.set()
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(own_source)
        assert held.wait(timeout=5)
        try:
            with pytest.raises(FileLockTimeout):
                index.initialize(deadline=time.monotonic() + 2.0)
            assert not index.failure_path.exists()
        finally:
            release.set()
        owner.result()
    assert index.spawns() == ()


def test_status_and_counts_do_not_build_or_drain(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from meridian.lib.ops import session_index
    from meridian.lib.state.history_changes import HistoryChanges, HistorySource

    monkeypatch.setattr(
        session_index,
        "resolve_roots_for_read",
        lambda _: SimpleNamespace(runtime_root=tmp_path, project_root=tmp_path),
    )
    index = HistoryIndex(tmp_path)
    assert session_index.session_index_sync(session_index.SessionIndexInput()).baseline == "absent"
    from meridian.lib.harness.transcript_preview import TRANSCRIPT_PREVIEW_VERSION

    assert index.preview_count(preview_version=TRANSCRIPT_PREVIEW_VERSION) == 0
    assert not index.directory.exists()
    index.rebuild()
    changes = HistoryChanges(tmp_path)
    changes.mark(HistorySource(kind="spawn", key="p99"))
    before = changes.inspect()
    output = session_index.session_index_sync(session_index.SessionIndexInput())
    assert output.baseline == "current" and output.coverage is None
    assert output.pending_sources == 1
    assert changes.inspect() == before


@pytest.mark.parametrize("foreign", [2, "next"])
def test_foreign_schema_in_this_file_is_read_only_and_never_hydrated(
    tmp_path: Path, foreign: int | str
) -> None:
    """Only this schema writes its file: another version is a copy, not an upgrade."""
    import sqlite3

    from meridian.lib.state import spawn_store

    spawn_store.start_spawn(
        tmp_path, chat_id="c1", prompt="hello", model="test", agent="coder", harness="codex"
    )
    index = HistoryIndex(tmp_path)
    index.rebuild()
    version = history_index.SCHEMA_VERSION + 1 if foreign == "next" else foreign
    with sqlite3.connect(index.path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("UPDATE meta SET version=?", (version,))
        db.execute("UPDATE records SET record_json = 'not-json'")
    original = index.path.read_bytes()
    status = index.classify(deadline=time.monotonic() + 2)
    assert status.baseline == "incompatible" and status.schema == version
    assert status.reason == f"Index schema {version} does not match {index.path.name}."
    with pytest.raises(
        history_index.HistoryIndexIncomplete,
        match=r"incompatible.*meridian session index rebuild --metadata-only",
    ):
        index.spawns()
    assert index.path.read_bytes() == original
    assert not index.failure_path.exists()
    assert index.rebuild().complete
    assert index.inspect().baseline == "current"


def test_explicit_deadline_does_not_start_implicit_initialization(tmp_path: Path) -> None:
    index = HistoryIndex(tmp_path)
    with (
        pytest.raises(history_index.HistoryIndexIncomplete, match="initialization/rebuild"),
        index.query(deadline=time.monotonic() + 0.1),
    ):
        pytest.fail("Absent index must not be queried")
    assert not index.path.exists() and not index.failure_path.exists()


def test_malformed_marker_requires_manual_recovery(tmp_path: Path) -> None:
    index = HistoryIndex(tmp_path)
    index.failure_path.write_text("{broken")
    with pytest.raises(history_index.HistoryIndexIncomplete, match="Corrupt initialization"):
        index.spawns()
    assert not index.path.exists()
    assert index.rebuild().complete
    assert not index.failure_path.exists()


def test_failure_marker_write_and_clear_failures_are_truthful(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)

    def fail_projection(*args):
        raise ValueError("private raw payload")

    def fail_marker(*args):
        raise PermissionError("blocked marker")

    with monkeypatch.context() as patch:
        patch.setattr(HistoryIndex, "_project", fail_projection)
        patch.setattr(history_index, "atomic_write_text", fail_marker)
        with pytest.raises(history_index.HistoryIndexIncomplete, match="retries may recur"):
            index.spawns()
    assert not index.failure_path.exists()
    index.failure_path.write_text("{broken")
    original_unlink = Path.unlink

    def fail_clear(path, *args, **kwargs):
        if path == index.failure_path:
            raise PermissionError("cannot clear")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_clear)
        coverage = index.rebuild()
        assert coverage.complete and coverage.warnings
        assert index.inspect().baseline == "current"
    assert index.failure_path.exists()  # Status stays read-only, even after permission repair.
    assert index.spawns() == ()
    assert not index.failure_path.exists()  # Warm catch-up repairs deferred cleanup.
    index.path.unlink()
    assert index.spawns() == ()  # Disposable DB loss must not revive a pre-success latch.


def test_published_index_with_uncertain_fsync_is_not_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    index = HistoryIndex(tmp_path)
    original_sync = history_index.fsync_directory

    def fail_directory_sync(path):
        if path == index.directory:
            raise OSError("directory sync failed")
        original_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(history_index, "fsync_directory", fail_directory_sync)
        with pytest.raises(history_index.HistoryIndexIncomplete, match="I/O failure"):
            index.spawns()
    assert index.failure_path.exists() and index.path.exists()
    build = index.inspect().build
    assert index.spawns() == ()
    assert index.inspect().build == build


def test_failure_after_publication_does_not_latch_initialization(
    tmp_path: Path, monkeypatch
) -> None:
    from meridian.lib.state.history_changes import HistoryChanges, HistorySource

    index = HistoryIndex(tmp_path)
    HistoryChanges(tmp_path).mark(HistorySource(kind="spawn", key="p99"))

    def lose_ack(*args):
        raise OSError("lost acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(HistoryChanges, "acknowledge", lose_ack)
        with pytest.raises(OSError, match="lost acknowledgement"):
            index.spawns()
    assert index.path.exists() and not index.failure_path.exists()
    assert index.spawns() == ()


def test_published_build_needs_no_staging_unlink(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)
    original_unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(index.stage.name) and index.path.exists():
            raise PermissionError("post-publication staging cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    assert index.spawns() == ()
    assert not index.failure_path.exists()


def test_staging_cleanup_failure_does_not_replace_cancellation(tmp_path: Path, monkeypatch) -> None:
    index = HistoryIndex(tmp_path)
    original_unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path == index.stage and path.exists():
            raise PermissionError("pre-publication staging cleanup failed")
        return original_unlink(path, *args, **kwargs)

    def cancel(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cleanup)
        patch.setattr(HistoryIndex, "_project", cancel)
        with pytest.raises(KeyboardInterrupt):
            index.spawns()
    assert not index.failure_path.exists() and not index.path.exists()
    assert index.spawns() == ()  # Next owner removes disposable staging residue.


def test_session_projection_matches_authority_for_nonobject_lines(tmp_path: Path) -> None:
    from meridian.lib.state import session_store

    (tmp_path / "sessions.jsonl").write_text('[]\nnull\n{"event":"unknown"}\n')
    assert session_store.list_session_generations(tmp_path) == ()
    assert HistoryIndex(tmp_path).sessions() == []


def test_corpus_shares_one_cold_budget_and_does_not_latch_skipped_roots(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from meridian.lib.ops import session_search
    from meridian.lib.ops.session_corpus import SessionCorpusScope

    roots = [tmp_path / str(number) for number in range(3)]
    scopes = tuple(SessionCorpusScope(tmp_path, root, str(root)) for root in roots)
    monkeypatch.setattr(
        session_search,
        "resolve_roots_for_read",
        lambda _: SimpleNamespace(project_root=tmp_path, runtime_root=roots[0]),
    )
    monkeypatch.setattr(session_search, "resolve_session_search_corpus", lambda **_: scopes)
    monkeypatch.setattr(session_search, "INITIALIZATION_TIMEOUT", 0.4)
    elapsed = 0.0
    started = time.monotonic()
    clock = SimpleNamespace(monotonic=lambda: started + elapsed)
    monkeypatch.setattr(history_index, "time", clock)
    monkeypatch.setattr(session_search, "time", clock)
    original_project = HistoryIndex._project

    def slow_project(self, db, source):
        nonlocal elapsed
        if source.kind == "sessions":
            elapsed += 0.25
        return original_project(self, db, source)

    monkeypatch.setattr(HistoryIndex, "_project", slow_project)
    output = session_search.session_search_sync(
        session_search.SessionSearchInput(query="missing", work_id="work")
    )
    assert not output.complete
    assert HistoryIndex(roots[0]).path.exists()
    assert HistoryIndex(roots[1]).failure_path.exists()  # This root owned an exhausted build.
    assert not HistoryIndex(roots[2]).failure_path.exists()  # Never attempted.
    assert len(output.errors) == 2


def test_all_warm_corpus_does_not_reset_its_deadline_after_classification(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from meridian.lib.ops import session_search
    from meridian.lib.ops.session_corpus import SessionCorpusScope

    roots = [tmp_path / str(number) for number in range(2)]
    from meridian.lib.state.native_search_index import NativeSearchIndex

    for root in roots:
        HistoryIndex(root).rebuild()
        NativeSearchIndex.for_runtime(root)
    scopes = tuple(SessionCorpusScope(tmp_path, root, str(root)) for root in roots)
    monkeypatch.setattr(
        session_search,
        "resolve_roots_for_read",
        lambda _: SimpleNamespace(project_root=tmp_path, runtime_root=roots[0]),
    )
    monkeypatch.setattr(session_search, "resolve_session_search_corpus", lambda **_: scopes)
    monkeypatch.setattr(session_search, "QUERY_TIMEOUT", 0.1)
    elapsed = 0.0
    started = time.monotonic()
    clock = SimpleNamespace(monotonic=lambda: started + elapsed)
    monkeypatch.setattr(history_index, "time", clock)
    monkeypatch.setattr(session_search, "time", clock)
    original_classify = HistoryIndex.classify

    def slow_classify(self, *, deadline):
        nonlocal elapsed
        result = original_classify(self, deadline=deadline)
        elapsed += 0.06
        return result

    monkeypatch.setattr(HistoryIndex, "classify", slow_classify)
    output = session_search.session_search_sync(
        session_search.SessionSearchInput(query="missing", work_id="work")
    )
    assert not output.complete and not output.truncated
    assert all(not HistoryIndex(root).failure_path.exists() for root in roots)
