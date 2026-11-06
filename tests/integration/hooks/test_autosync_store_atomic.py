from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from meridian.lib.hooks.builtin.autosync_store import (
    ConflictRecord,
    conflict_dir,
    read_conflicts,
    transaction,
)


def test_hook_write_and_resolution_do_not_lose_conflict_update(tmp_path: Path) -> None:
    """Resolution waits for the hook transaction that creates its record."""
    runtime_root = tmp_path / "runtime"

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
        with transaction(tmp_path, runtime_root=runtime_root) as autosync_tx:
            autosync_tx.write_conflict(record)
            record_written.set()
            assert release_hook.wait(timeout=5)
            autosync_tx.write_sync_state(outcome="conflict_detected", conflict_id="c1")

    def resolve_workflow() -> None:
        assert record_written.wait(timeout=5)
        with transaction(tmp_path, runtime_root=runtime_root) as autosync_tx:
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

    [stored] = read_conflicts(tmp_path, runtime_root=runtime_root)
    assert stored.resolved is True
    assert stored.resolved_at is not None


def test_resolution_preserves_idless_document_shape_and_hides_unknown_fields(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    conflicts = conflict_dir(tmp_path, runtime_root=runtime_root)
    conflicts.mkdir(parents=True)
    target = conflicts / "c2.json"
    target.write_text(
        json.dumps(
            {
                "context": "work",
                "trigger": {"event": "work.done"},
                "custom": {"retained": True},
            }
        ),
        encoding="utf-8",
    )

    with transaction(tmp_path, runtime_root=runtime_root) as autosync_tx:
        assert autosync_tx.mark_resolved("c2")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert set(payload) == {"context", "trigger", "custom", "resolved", "resolved_at"}
    assert payload["custom"] == {"retained": True}
    assert payload["resolved"] is True
    assert isinstance(payload["resolved_at"], str)

    [record] = read_conflicts(tmp_path, runtime_root=runtime_root)
    assert record.id == "c2"
    assert record.sync_root == tmp_path.as_posix()
    assert "custom" not in record.model_dump()


