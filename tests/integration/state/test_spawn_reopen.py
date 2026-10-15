"""Run allocation is serialized, generation-free, and independent of suffix holes."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.spawn_lifecycle import SpawnReservation
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.ops.reference import resolve_continue_source_row
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn import repository as spawn_repository
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.runs import SpawnBusy, latest_bound_run, latest_run
from tests.support.process_race import run_spawn_race_or_skip


def _bind_started_session(runtime_root: Path, run_id: RunId, chat_id: str) -> None:
    record = session_store.get_session_record(runtime_root, chat_id)
    assert record is not None
    spawn_store.bind_run_generation(
        runtime_root,
        run_id,
        chat_id=chat_id,
        session_instance_id=record.session_instance_id,
        forked_from_history_id=record.forked_from_history_id,
    )


def _reserve_run_without_busy_check_worker(runtime_root: str) -> str:
    """Exercise allocation contention without the one-active-run policy."""

    def current_run(root: Path, spawn_id: SpawnId, chat_id: str | None = None) -> RunId:
        del chat_id
        run_id = latest_run(root, spawn_id)
        assert run_id is not None
        return run_id

    spawn_store.check_spawn_available = current_run
    reservation = SpawnReservation(
        chat_id="c1",
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )
    return str(spawn_store.reserve_run(Path(runtime_root), SpawnId("p1"), reservation))


@pytest.mark.parametrize('status,live,busy', [
    ('succeeded', False, False), ('running', False, False),
    ('running', True, True), ('queued', True, True),
])
def test_reserve_run_contract(tmp_path: Path, status: str, live: bool, busy: bool) -> None:
    first = spawn_store.start_spawn(
        tmp_path, spawn_id='p1', chat_id='c1', model='test', agent='', harness='codex', prompt='hi',
    )
    session_store.start_session(
        tmp_path, harness='codex', harness_session_id='native', model='test',
        chat_id='c1', spawn_id=first,
    )
    _bind_started_session(tmp_path, first, "c1")
    session_store.stop_session(tmp_path, 'c1')
    spawn_store.finalize_spawn(tmp_path, first, status='succeeded', exit_code=0, origin='runner')
    latest = spawn_store.start_spawn(
        tmp_path, spawn_id='p1.3', chat_id='c1', owner_chat_id='c9', model='test', agent='',
        harness='codex', prompt='hi', started_at='2000-01-01T00:00:00Z',
        runner_pid=os.getpid() if live else 999_999_999,
    )
    if status == 'succeeded':
        spawn_store.finalize_spawn(
            tmp_path, latest, status='succeeded', exit_code=0, origin='runner',
        )
    elif status == 'queued':
        # The publisher's PID keeps a queued reservation live beyond startup grace.
        from meridian.lib.state.spawn.repository import write_state_locked
        write_state_locked(tmp_path / 'spawns', latest,
                           lambda row: row.model_copy(update={'status': SpawnStatus.QUEUED}))
    reservation = SpawnReservation(
        chat_id='c1', owner_chat_id='c9', kind='primary', prompt='again',
        session_metadata=PrimarySessionMetadata(
            harness='codex', model='test', agent='', agent_path='', skills=(), skill_paths=(),
        ),
    )
    if busy:
        with pytest.raises(ValueError, match=r'busy.*p1\.3'):
            spawn_store.reserve_run(tmp_path, SpawnId('p1'), reservation)
        return
    new = spawn_store.reserve_run(tmp_path, SpawnId('p1'), reservation)
    assert new == 'p1.4'
    row = spawn_store.get_spawn(tmp_path, new)
    assert row is not None
    assert (row.session_instance_id, row.forked_from_history_id) == (None, None)
    assert row.runner_pid == os.getpid() and row.runner_created_at_epoch is not None
    assert (row.chat_id, row.owner_chat_id) == ('c1', 'c9')
    bound = spawn_store.get_spawn(tmp_path, parse_ref('p1', row_key=True))
    assert bound is not None
    assert latest_bound_run(tmp_path, SpawnId("p1")) == bound.id


def test_missing_head_recovers_across_holes_before_continue_and_reservation(
    tmp_path: Path,
) -> None:
    metadata = PrimarySessionMetadata(
        harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
    )
    first = spawn_store.start_spawn(
        tmp_path, spawn_id="p1", chat_id="c1", model="test", agent="",
        harness="codex", prompt="hi",
    )
    session_store.start_session(
        tmp_path, harness="codex", harness_session_id="native", model="test",
        chat_id="c1", spawn_id=first,
    )
    _bind_started_session(tmp_path, first, "c1")
    session_store.stop_session(tmp_path, "c1")
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )

    runs: list[RunId] = []
    for _ in range(2):
        run_id = spawn_store.reserve_run(
            tmp_path,
            SpawnId("p1"),
            SpawnReservation(
                chat_id="c1", kind="primary", prompt="again", session_metadata=metadata,
            ),
        )
        session_store.start_session(
            tmp_path, harness="codex", harness_session_id="native", model="test",
            chat_id="c1", spawn_id=run_id, reopen=True,
        )
        _bind_started_session(tmp_path, run_id, "c1")
        session_store.stop_session(tmp_path, "c1")
        spawn_store.finalize_spawn(
            tmp_path, run_id, status="succeeded", exit_code=0, origin="runner",
        )
        runs.append(run_id)

    assert spawn_store.delete_published_spawn(
        tmp_path, runs[0], can_delete=lambda _row: True,
    )
    (tmp_path / "spawn-heads" / "p1").unlink()

    assert resolve_continue_source_row(tmp_path, parse_ref("p1")).id == runs[1]
    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "3\n"
    live = spawn_store.reserve_run(
        tmp_path,
        SpawnId("p1"),
        SpawnReservation(
            chat_id="c1", kind="primary", prompt="again", session_metadata=metadata,
        ),
    )
    assert live == "p1.4"
    with pytest.raises(SpawnBusy, match=r"busy.*p1\.4"):
        spawn_store.reserve_run(
            tmp_path,
            SpawnId("p1"),
            SpawnReservation(
                chat_id="c1", kind="primary", prompt="again", session_metadata=metadata,
            ),
        )


def test_concurrent_run_reservations_allocate_distinct_suffixes(tmp_path: Path) -> None:
    first = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="",
        harness="codex",
        prompt="hi",
    )
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )

    allocated = run_spawn_race_or_skip(
        _reserve_run_without_busy_check_worker,
        [(tmp_path.as_posix(),), (tmp_path.as_posix(),)],
    )

    assert sorted(allocated) == ["p1.2", "p1.3"]


def test_interrupted_publish_leaves_pointer_that_recovers_to_published_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="",
        harness="codex",
        prompt="hi",
    )
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise RuntimeError("simulated crash before run publication")

    monkeypatch.setattr(spawn_store, "atomic_publish_dir", fail_publish)
    reservation = SpawnReservation(
        chat_id="c1",
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation)

    assert (tmp_path / "spawn-heads" / "p1").read_text(encoding="utf-8") == "2\n"
    assert latest_run(tmp_path, SpawnId("p1")) == first


def test_reserve_run_checks_ancestors_without_listing_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, parent in (("p1", None), ("p2", "p1"), ("p3", "p2")):
        run_id = spawn_store.start_spawn(
            tmp_path,
            spawn_id=key,
            chat_id=f"c{key[1:]}",
            parent_id=SpawnId(parent) if parent else None,
            parent_run_id=parse_ref(parent, row_key=True) if parent else None,
            model="test",
            agent="",
            harness="codex",
            prompt="hi",
        )
        spawn_store.finalize_spawn(
            tmp_path, run_id, status="succeeded", exit_code=0, origin="runner",
        )

    monkeypatch.setattr(
        spawn_store,
        "list_spawns",
        lambda *_args, **_kwargs: pytest.fail("ancestor check listed all spawns"),
    )
    reservation = SpawnReservation(
        chat_id="c1",
        parent_id="p3",
        parent_run_id=parse_ref("p3", row_key=True),
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )

    with pytest.raises(ValueError, match="Cannot reopen ancestor spawn p1"):
        spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation)


def test_reserve_run_does_not_scan_for_a_fully_archived_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("p1", "p2"):
        run_id = spawn_store.start_spawn(
            tmp_path,
            spawn_id=key,
            chat_id=f"c{key[1:]}",
            model="test",
            agent="",
            harness="codex",
            prompt="hi",
        )
        spawn_store.finalize_spawn(
            tmp_path, run_id, status="succeeded", exit_code=0, origin="runner",
        )
    assert spawn_store.delete_published_spawn(
        tmp_path, parse_ref("p2", row_key=True), can_delete=lambda _row: True,
    )
    scans = 0
    scan = spawn_repository.scan_spawn_ids

    def count_scan(spawns_dir: Path) -> list[RunId]:
        nonlocal scans
        scans += 1
        return scan(spawns_dir)

    monkeypatch.setattr(spawn_repository, "scan_spawn_ids", count_scan)
    reservation = SpawnReservation(
        chat_id="c1",
        parent_id="p2",
        parent_run_id=parse_ref("p2", row_key=True),
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )

    assert spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation) == "p1.2"
    assert scans == 0


def test_reserve_run_checks_parent_edges_from_non_latest_runs(tmp_path: Path) -> None:
    metadata = PrimarySessionMetadata(
        harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
    )

    for key, parent in (("p1", None), ("p2", "p1"), ("p3", None)):
        run_id = spawn_store.start_spawn(
            tmp_path,
            spawn_id=key,
            chat_id=f"c{key[1:]}",
            parent_id=SpawnId(parent) if parent else None,
            parent_run_id=parse_ref(parent, row_key=True) if parent else None,
            model="test",
            agent="",
            harness="codex",
            prompt="hi",
        )
        spawn_store.finalize_spawn(
            tmp_path, run_id, status="succeeded", exit_code=0, origin="runner",
        )

    second = spawn_store.reserve_run(
        tmp_path,
        SpawnId("p2"),
        SpawnReservation(
            chat_id="c2",
            parent_id="p3",
            parent_run_id=parse_ref("p3", row_key=True),
            prompt="move",
            session_metadata=metadata,
        ),
    )
    spawn_store.finalize_spawn(
        tmp_path, second, status="succeeded", exit_code=0, origin="runner",
    )

    with pytest.raises(ValueError, match="Cannot reopen ancestor spawn p1"):
        spawn_store.reserve_run(
            tmp_path,
            SpawnId("p1"),
            SpawnReservation(
                chat_id="c1",
                parent_id="p2",
                prompt="cycle",
                session_metadata=metadata,
            ),
        )


def test_reserve_run_reads_one_spawn_row_while_allocation_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="",
        harness="codex",
        prompt="hi",
    )
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )
    paths = spawn_store.RuntimePaths.from_root_dir(tmp_path)
    original_lock_file = spawn_store.lock_file
    original_read_state = spawn_repository.read_state
    allocation_lock_held = False
    locked_reads: list[str] = []

    @contextmanager
    def tracked_lock_file(path: Path, *args: Any, **kwargs: Any) -> Iterator[None]:
        nonlocal allocation_lock_held
        with original_lock_file(path, *args, **kwargs):
            was_held = allocation_lock_held
            if path == paths.spawns_flock:
                allocation_lock_held = True
            try:
                yield
            finally:
                allocation_lock_held = was_held

    def tracked_read_state(*args: Any, **kwargs: Any) -> Any:
        if allocation_lock_held:
            locked_reads.append(str(args[1]))
        return original_read_state(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "lock_file", tracked_lock_file)
    monkeypatch.setattr(spawn_repository, "read_state", tracked_read_state)
    reservation = SpawnReservation(
        chat_id="c1",
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )

    assert spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation) == "p1.2"
    assert locked_reads == ["p1"]


def test_spawn_started_at_is_stamped_under_the_allocation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="",
        harness="codex",
        prompt="hi",
    )
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )
    paths = spawn_store.RuntimePaths.from_root_dir(tmp_path)
    original_lock_file = spawn_store.lock_file
    allocation_lock_held = False

    @contextmanager
    def tracked_lock_file(path: Path, *args: Any, **kwargs: Any) -> Iterator[None]:
        nonlocal allocation_lock_held
        with original_lock_file(path, *args, **kwargs):
            was_held = allocation_lock_held
            if path == paths.spawns_flock:
                allocation_lock_held = True
            try:
                yield
            finally:
                allocation_lock_held = was_held

    class LockAssertingClock:
        def __init__(self) -> None:
            self.calls = 0

        def utc_now_iso(self) -> str:
            assert allocation_lock_held
            self.calls += 1
            return f"2026-10-15T00:00:0{self.calls}Z"

    clock = LockAssertingClock()
    monkeypatch.setattr(spawn_store, "lock_file", tracked_lock_file)

    fresh = spawn_store.start_spawn(
        tmp_path,
        chat_id="c2",
        model="test",
        agent="",
        harness="codex",
        prompt="fresh",
        clock=clock,
    )
    reopened = spawn_store.reserve_run(
        tmp_path,
        SpawnId("p1"),
        SpawnReservation(
            chat_id="c1",
            prompt="again",
            clock=clock,
            session_metadata=PrimarySessionMetadata(
                harness="codex", model="test", agent="", agent_path="",
                skills=(), skill_paths=(),
            ),
        ),
    )

    fresh_row = spawn_store.get_spawn(tmp_path, fresh)
    reopened_row = spawn_store.get_spawn(tmp_path, reopened)
    assert fresh_row is not None and fresh_row.started_at == "2026-10-15T00:00:01Z"
    assert reopened_row is not None and reopened_row.started_at == "2026-10-15T00:00:02Z"


def test_primary_run_reservation_does_not_reread_owner_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test",
        agent="",
        harness="codex",
        prompt="hi",
    )
    spawn_store.finalize_spawn(
        tmp_path, first, status="succeeded", exit_code=0, origin="runner",
    )
    monkeypatch.setattr(
        session_store,
        "get_session_record",
        lambda *_args, **_kwargs: pytest.fail("reopen reread the owner session"),
    )
    reservation = SpawnReservation(
        chat_id="c1",
        kind="primary",
        prompt="again",
        session_metadata=PrimarySessionMetadata(
            harness="codex", model="test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
    )

    assert spawn_store.reserve_run(tmp_path, SpawnId("p1"), reservation) == "p1.2"


def test_start_spawn_resolves_exact_parent_before_allocation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_parent = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c-parent",
        model="test",
        agent="",
        harness="codex",
        prompt="first parent",
    )
    spawn_store.finalize_spawn(
        tmp_path, first_parent, status="succeeded", exit_code=0, origin="runner",
    )
    later_parent = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1.2",
        chat_id="c-parent",
        model="test",
        agent="",
        harness="codex",
        prompt="later parent",
    )
    spawn_store.finalize_spawn(
        tmp_path, later_parent, status="succeeded", exit_code=0, origin="runner",
    )
    first_parent_row = spawn_store.get_spawn(tmp_path, first_parent)
    assert first_parent_row is not None

    paths = spawn_store.RuntimePaths.from_root_dir(tmp_path)
    original_lock_file = spawn_store.lock_file
    original_read_state = spawn_store._read_state
    allocation_lock_held = False
    locked_dependency_reads: list[str] = []

    @contextmanager
    def tracked_lock_file(path: Path, *args: Any, **kwargs: Any) -> Iterator[None]:
        nonlocal allocation_lock_held
        with original_lock_file(path, *args, **kwargs):
            was_held = allocation_lock_held
            if path == paths.spawns_flock:
                allocation_lock_held = True
            try:
                yield
            finally:
                allocation_lock_held = was_held

    def tracked_read_state(
        spawns_dir: Path,
        spawn_id: RunId,
        *,
        include_prompt: bool = True,
    ) -> SpawnRecord | None:
        if allocation_lock_held:
            locked_dependency_reads.append(f"parent:{spawn_id}")
        return original_read_state(
            spawns_dir, spawn_id, include_prompt=include_prompt,
        )

    monkeypatch.setattr(spawn_store, "lock_file", tracked_lock_file)
    monkeypatch.setattr(spawn_store, "_read_state", tracked_read_state)

    child = spawn_store.start_spawn(
        tmp_path,
        chat_id="c-owner",
        parent_id=SpawnId("p1"),
        parent_run_id=first_parent,
        model="test",
        agent="",
        harness="codex",
        kind="primary",
        prompt="child",
    )
    row = spawn_store.get_spawn(tmp_path, child)

    assert locked_dependency_reads == []
    assert row is not None
    assert row.session_instance_id is None
    assert row.parent_history_id == first_parent_row.history_id
