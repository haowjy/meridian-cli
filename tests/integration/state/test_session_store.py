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

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.state import session_store
from meridian.lib.state.native_binding import Bound, Conflict, Same
from tests.conftest import posix_only


def _crash_session_cleanup_during_append(runtime_root: Path) -> None:
    def append_partial_then_crash(
        root: Path,
        _batches: object,
    ) -> None:
        with (root / "sessions.jsonl").open("ab") as handle:
            handle.write(b'{"event":"stop","session_instance_id"')
            handle.flush()
            os.fsync(handle.fileno())
        os.kill(os.getpid(), signal.SIGKILL)

    session_store.append_chat_rows = append_partial_then_crash  # type: ignore[assignment]
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


@posix_only
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
            harness_session_id="c10-thread",
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
        lease = session_store._read_session_lease_data(
            session_store.RuntimePaths.from_root_dir(runtime_root), chat_id
        )
        assert lease is not None
        assert lease.session_instance_id == restarted[0].session_instance_id
        assert lease.owner_created_at_epoch is not None
    finally:
        session_store._SESSION_LOCK_HANDLES.pop(registry_key, None)
        session_store.release_file_lock(restarted[0].session)
        session_store.release_file_lock(restarted[0].project_lifetime)


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX subprocess and SIGKILL")
def test_cleanup_repairs_torn_event_tail_before_retrying_stop(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = "c-torn"
    generation = "torn-generation"
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
        target=_crash_session_cleanup_during_append,
        args=(runtime_root,),
    )
    process.start()
    process.join(timeout=5)
    assert process.exitcode == -signal.SIGKILL
    assert (
        (runtime_root / "sessions.jsonl")
        .read_bytes()
        .endswith(b'{"event":"stop","session_instance_id"')
    )

    session_store.cleanup_stale_sessions(runtime_root)

    record = session_store.chat_record(runtime_root, chat_id)
    assert record is not None
    assert record.stopped_at is not None
    assert not lock_path.exists()
    assert not lease_path.exists()


def test_native_binding_is_immutable(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id="",
        model="test",
    ).chat_id
    try:
        first = session_store.update_session_harness_id(
            runtime_root,
            chat_id,
            NativeKeyFields(session_id="first", native_store="/native"),
            source="assigned",
        )
        assert isinstance(first, Bound)
        bound_bytes = (runtime_root / "sessions.jsonl").read_bytes()
        assert isinstance(
            session_store.update_session_harness_id(
                runtime_root,
                chat_id,
                NativeKeyFields(session_id="first", native_store="/native"),
                source="observed",
            ),
            Same,
        )
        before = session_store.chat_record(runtime_root, chat_id)
        conflict = session_store.update_session_harness_id(
            runtime_root,
            chat_id,
            NativeKeyFields(session_id="other", native_store="/other"),
            source="observed",
        )
        assert isinstance(conflict, Conflict)
        assert conflict.kept.session_id == "first"
        assert isinstance(
            session_store.update_session_harness_id(
                runtime_root,
                chat_id,
                NativeKeyFields(session_id="first", native_store="/other"),
                source="observed",
            ),
            Conflict,
        )
        assert session_store.chat_record(runtime_root, chat_id) == before
        assert (runtime_root / "sessions.jsonl").read_bytes() == bound_bytes
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_concurrent_native_binding_has_one_winner(tmp_path: Path) -> None:
    from tests.support.process_race import run_spawn_race_or_skip

    root = _state_root(tmp_path)
    chat_id = session_store.start_session(root, "claude", "", "sonnet").chat_id
    try:
        results = run_spawn_race_or_skip(
            _race_bind,
            [
                (root, chat_id, NativeKeyFields(session_id="first")),
                (root, chat_id, NativeKeyFields(session_id="second")),
            ],
        )
        assert sorted(type(result).__name__ for result in results) == ["Bound", "Conflict"]
        winner = next(result.key for result in results if isinstance(result, Bound))
        loser = next(result.kept for result in results if isinstance(result, Conflict))
        assert winner == loser
        assert session_store.get_session_harness_id(root, chat_id) == winner.session_id
    finally:
        session_store.stop_session(root, chat_id)


def _race_bind(root, chat_id, attempted):
    return session_store.update_session_harness_id(root, chat_id, attempted, source="observed")
