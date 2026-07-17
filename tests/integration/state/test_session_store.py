# qa-validated: test-suite-redesign

"""Session store CRUD, cleanup, and record-reading tests.

Covers: start/stop lifecycle, cleanup_stale_sessions generation rules,
pid-based primary-session cleanup, session record projection, and harness_id
update flows.  Cross-process lock safety lives in test_session_concurrency.py.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
from pathlib import Path

import pytest

from meridian.lib.state import session_store


def _crash_session_cleanup(runtime_root: Path, crash_stage: str) -> None:
    original_unlink = session_store.unlink_validated_lock

    def crash_at_unlink(lock_path: Path, handle: object) -> bool:
        if crash_stage == "before_unlink":
            os.kill(os.getpid(), signal.SIGKILL)
        removed = original_unlink(lock_path, handle)  # type: ignore[arg-type]
        os.kill(os.getpid(), signal.SIGKILL)
        return removed

    session_store.unlink_validated_lock = crash_at_unlink  # type: ignore[assignment]
    session_store.cleanup_stale_sessions(runtime_root)


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _write_session_start(
    *,
    runtime_root: Path,
    chat_id: str,
    session_instance_id: str,
    harness: str = "codex",
    kind: str = "spawn",
) -> None:
    with (runtime_root / "sessions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "start",
                    "chat_id": chat_id,
                    "kind": kind,
                    "harness": harness,
                    "harness_session_id": f"{chat_id}-thread",
                    "model": "gpt-5.4",
                    "session_instance_id": session_instance_id,
                    "started_at": "2026-03-01T00:00:00Z",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )


def test_start_session_does_not_append_start_event_when_lock_acquire_fails(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    sessions_dir_as_file = runtime_root / "sessions"
    sessions_dir_as_file.write_text("not-a-directory\n", encoding="utf-8")

    with pytest.raises(OSError):
        session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id="thread-1",
            model="gpt-5.4",
        )

    assert not (runtime_root / "sessions.jsonl").exists()


@pytest.mark.parametrize(
    (
        "chat_id",
        "harness",
        "start_generation",
        "lease_generation",
        "expected_cleaned",
        "expected_materialized",
        "expect_lock_exists",
        "expect_lease_exists",
        "expected_stop_count",
    ),
    [
        pytest.param(
            "c7",
            "codex",
            "new-generation",
            "old-generation",
            (),
            (),
            True,
            True,
            0,
            id="skips-generation-mismatch",
        ),
        pytest.param(
            "c8",
            "claude",
            "matching-generation",
            "matching-generation",
            ("c8",),
            ("claude",),
            False,
            False,
            1,
            id="stops-and-cleans-when-generation-matches",
        ),
    ],
)
def test_cleanup_stale_sessions_handles_generation_rules(
    tmp_path: Path,
    chat_id: str,
    harness: str,
    start_generation: str,
    lease_generation: str,
    expected_cleaned: tuple[str, ...],
    expected_materialized: tuple[str, ...],
    expect_lock_exists: bool,
    expect_lease_exists: bool,
    expected_stop_count: int,
) -> None:
    runtime_root = _state_root(tmp_path)
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_instance_id=start_generation,
        harness=harness,
    )

    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    lease_path = runtime_root / "sessions" / f"{chat_id}.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": 123,
                "session_instance_id": lease_generation,
            }
        ),
        encoding="utf-8",
    )

    cleanup = session_store.cleanup_stale_sessions(runtime_root)

    assert cleanup.cleaned_ids == expected_cleaned
    assert cleanup.materialized_scopes == expected_materialized
    assert lock_path.exists() is expect_lock_exists
    assert lease_path.exists() is expect_lease_exists

    rows = [
        json.loads(line)
        for line in (runtime_root / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stop_rows = [
        row for row in rows if row.get("event") == "stop" and row.get("chat_id") == chat_id
    ]
    assert len(stop_rows) == expected_stop_count
    if expected_stop_count == 1:
        assert stop_rows[0]["session_instance_id"] == start_generation


def test_cleanup_stale_primary_session_with_dead_pid_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c9"
    session_generation = "primary-generation"
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_instance_id=session_generation,
        harness="claude",
        kind="primary",
    )

    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    lease_path = runtime_root / "sessions" / f"{chat_id}.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": 43210,
                "session_instance_id": session_generation,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(session_store, "is_process_alive", lambda _pid: False)

    cleanup = session_store.cleanup_stale_sessions(runtime_root)
    assert cleanup.cleaned_ids == (chat_id,)
    assert cleanup.materialized_scopes == ("claude",)
    assert not lock_path.exists()
    assert not lease_path.exists()

    rows = [
        json.loads(line)
        for line in (runtime_root / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stop_rows = [
        row for row in rows if row.get("event") == "stop" and row.get("chat_id") == chat_id
    ]
    assert len(stop_rows) == 1
    assert stop_rows[0]["session_instance_id"] == session_generation


def test_cleanup_cannot_delete_concurrently_restarted_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c10"
    old_generation = "old-generation"
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_instance_id=old_generation,
    )
    sessions_dir = runtime_root / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / f"{chat_id}.lock").touch()
    (sessions_dir / f"{chat_id}.lease.json").write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": 999_999,
                "session_instance_id": old_generation,
            }
        ),
        encoding="utf-8",
    )

    original_unlink = session_store.unlink_validated_lock
    restarted: list[session_store._SessionLockHandles] = []

    def restart_after_unlink(lock_path: Path, handle: object) -> bool:
        removed = original_unlink(lock_path, handle)  # type: ignore[arg-type]
        session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id="new-thread",
            model="gpt-5.4",
            chat_id=chat_id,
        )
        restarted.append(
            session_store._SESSION_LOCK_HANDLES[
                session_store._session_lock_key(runtime_root, chat_id)
            ]
        )
        return removed

    monkeypatch.setattr(session_store, "unlink_validated_lock", restart_after_unlink)
    cleanup = session_store.cleanup_stale_sessions(runtime_root)

    registry_key = session_store._session_lock_key(runtime_root, chat_id)
    try:
        assert cleanup.cleaned_ids == (chat_id,)
        assert session_store._SESSION_LOCK_HANDLES.get(registry_key) is restarted[0]
        lease_exists, lease_generation, _owner_pid = session_store._read_session_lease_data(
            session_store.RuntimePaths.from_root_dir(runtime_root), chat_id
        )
        assert lease_exists
        assert lease_generation == restarted[0].session_instance_id
    finally:
        session_store._SESSION_LOCK_HANDLES.pop(registry_key, None)
        session_store.release_file_lock(restarted[0].session)
        session_store.release_file_lock(restarted[0].project_lifetime)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX fork and SIGKILL")
@pytest.mark.parametrize("crash_stage", ["before_unlink", "after_unlink"])
def test_cleanup_crash_remains_recoverable(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c11"
    generation = "crashed-generation"
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_instance_id=generation,
    )
    sessions_dir = runtime_root / "sessions"
    sessions_dir.mkdir(parents=True)
    lock_path = sessions_dir / f"{chat_id}.lock"
    lock_path.touch()
    lease_path = sessions_dir / f"{chat_id}.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": 999_999,
                "session_instance_id": generation,
            }
        ),
        encoding="utf-8",
    )

    process = multiprocessing.get_context("spawn").Process(
        target=_crash_session_cleanup,
        args=(runtime_root, crash_stage),
    )
    process.start()
    process.join(timeout=5)
    assert process.exitcode == -signal.SIGKILL

    session_store.cleanup_stale_sessions(runtime_root)

    assert not lock_path.exists()
    assert not lease_path.exists()
def test_empty_harness_session_id_flows_through_start_update_and_record(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="opencode",
        harness_session_id="",
        model="gpt-5.3-codex",
        chat_id="c42",
    )
    try:
        session_store.update_session_harness_id(runtime_root, chat_id, "resolved-thread")

        record = session_store.get_session_record(runtime_root, chat_id)
        assert record is not None
        assert record.harness_session_id == "resolved-thread"
        assert record.harness_session_ids == ("", "resolved-thread")

        rows = [
            json.loads(line)
            for line in (runtime_root / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows[0]["event"] == "start"
        assert rows[0]["harness_session_id"] == ""
        assert rows[1]["event"] == "update"
        assert rows[1]["harness_session_id"] == "resolved-thread"
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_cleanup_stale_primary_session_with_live_pid_is_not_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c10"
    session_generation = "primary-generation"
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=chat_id,
        session_instance_id=session_generation,
        harness="claude",
        kind="primary",
    )

    lock_path = runtime_root / "sessions" / f"{chat_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    lease_path = runtime_root / "sessions" / f"{chat_id}.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "owner_pid": 54321,
                "session_instance_id": session_generation,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(session_store, "is_process_alive", lambda _pid: True)

    cleanup = session_store.cleanup_stale_sessions(runtime_root)
    assert cleanup.cleaned_ids == ()
    assert cleanup.materialized_scopes == ()
    assert lock_path.exists()
    assert lease_path.exists()

    rows = [
        json.loads(line)
        for line in (runtime_root / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stop_rows = [
        row for row in rows if row.get("event") == "stop" and row.get("chat_id") == chat_id
    ]
    assert stop_rows == []


def test_cleanup_unlinks_cleaned_session_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _state_root(tmp_path)
    crashed_chat_id = "c11"
    live_chat_id = "c12"
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=crashed_chat_id,
        session_instance_id="crashed-generation",
    )
    _write_session_start(
        runtime_root=runtime_root,
        chat_id=live_chat_id,
        session_instance_id="live-generation",
        kind="primary",
    )

    sessions_dir = runtime_root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    crashed_lock = sessions_dir / f"{crashed_chat_id}.lock"
    live_lock = sessions_dir / f"{live_chat_id}.lock"
    crashed_lock.touch()
    live_lock.touch()
    (sessions_dir / f"{live_chat_id}.lease.json").write_text(
        json.dumps(
            {
                "chat_id": live_chat_id,
                "owner_pid": 54321,
                "session_instance_id": "live-generation",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_store, "is_process_alive", lambda _pid: True)

    cleanup = session_store.cleanup_stale_sessions(runtime_root)

    assert cleanup.cleaned_ids == (crashed_chat_id,)
    assert not crashed_lock.exists()
    assert live_lock.exists()
    assert session_store.cleanup_stale_sessions(runtime_root).cleaned_ids == ()
    assert tuple(sessions_dir.glob("*.lock")) == (live_lock,)


def test_records_by_session_ignores_mismatched_generation_stop_and_update(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    with (runtime_root / "sessions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "start",
                    "chat_id": "c10",
                    "harness": "codex",
                    "harness_session_id": "thread-1",
                    "model": "gpt-5.4",
                    "session_instance_id": "gen-a",
                    "started_at": "2026-03-01T00:00:00Z",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "update",
                    "chat_id": "c10",
                    "harness_session_id": "thread-2",
                    "session_instance_id": "gen-a",
                    "active_work_id": "work-1",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "update",
                    "chat_id": "c10",
                    "harness_session_id": "thread-ignored",
                    "session_instance_id": "gen-b",
                    "active_work_id": "work-ignored",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "event": "stop",
                    "chat_id": "c10",
                    "session_instance_id": "gen-b",
                    "stopped_at": "2026-03-01T00:01:00Z",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    record = session_store._records_by_session(runtime_root)["c10"]
    assert record.harness_session_id == "thread-2"
    assert record.harness_session_ids == ("thread-1", "thread-2")
    assert record.active_work_id == "work-1"
    assert record.forked_from_chat_id is None
    assert record.stopped_at is None
