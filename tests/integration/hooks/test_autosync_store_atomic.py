from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from meridian.lib.hooks.builtin.autosync_store import (
    ConflictRecord,
    autosync_lock_path,
    read_conflicts,
    transaction,
)


def test_local_and_remote_workflows_serialize_on_one_sync_root(tmp_path: Path) -> None:
    """Local and remote entry paths cannot overlap their mutation workflows."""

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_workflow() -> None:
        with transaction(tmp_path) as autosync_tx:
            first_entered.set()
            assert release_first.wait(timeout=5)
            autosync_tx.write_sync_state(outcome="local")

    def second_workflow() -> None:
        assert first_entered.wait(timeout=5)
        with transaction(tmp_path / ".") as autosync_tx:
            second_entered.set()
            autosync_tx.write_sync_state(outcome="remote")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_workflow)
        second = executor.submit(second_workflow)
        assert first_entered.wait(timeout=5)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.result()
        second.result()

    payload = json.loads(
        (tmp_path / ".meridian" / "autosync" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["outcome"] == "remote"


def test_hook_write_and_resolution_do_not_lose_conflict_update(tmp_path: Path) -> None:
    """Resolution waits for the hook transaction that creates its record."""

    record_written = threading.Event()
    release_hook = threading.Event()
    resolution_entered = threading.Event()
    record = ConflictRecord(
        id="c1",
        context="work",
        sync_root=str(tmp_path),
        conflict_type="content",
        paths=("shared.txt",),
        local_sha="local",
        remote_sha="remote",
        remote_branch="main",
        event_name="work.done",
        spawn_id=None,
        created_at="2026-07-17T00:00:00+00:00",
        resolved=False,
    )

    def hook_workflow() -> None:
        with transaction(tmp_path) as autosync_tx:
            autosync_tx.write_conflict(record)
            record_written.set()
            assert release_hook.wait(timeout=5)
            autosync_tx.write_sync_state(outcome="conflict_detected", conflict_id="c1")

    def resolve_workflow() -> None:
        assert record_written.wait(timeout=5)
        with transaction(tmp_path) as autosync_tx:
            resolution_entered.set()
            assert autosync_tx.mark_resolved("c1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        hook = executor.submit(hook_workflow)
        resolve = executor.submit(resolve_workflow)
        assert record_written.wait(timeout=5)
        assert not resolution_entered.wait(timeout=0.1)
        release_hook.set()
        hook.result()
        resolve.result()

    [stored] = read_conflicts(tmp_path)
    assert stored.resolved is True
    assert stored.resolved_at is not None


def test_lock_path_is_canonical_for_sync_root_aliases(tmp_path: Path) -> None:
    assert autosync_lock_path(tmp_path) == autosync_lock_path(tmp_path / ".")
