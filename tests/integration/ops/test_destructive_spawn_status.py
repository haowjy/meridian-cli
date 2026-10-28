"""Stored status protects archive selection while launch locks protect liveness."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pytest

from meridian.lib.ops import diag
from meridian.lib.ops import mars as mars_ops
from meridian.lib.ops.diag import DoctorInput, DoctorOutput, doctor_sync
from meridian.lib.ops.session_archive import (
    SessionArchiveInput,
    SessionArchiveOutput,
    materialize_native_history,
    session_archive_sync,
)
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.history_codec import transcript_header
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.native_snapshot import (
    NATIVE_SNAPSHOT_FILENAME,
    SnapshotHeader,
    SnapshotObservation,
    SnapshotRecord,
    SourceRevision,
    write_snapshot,
)
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.utils.json_shapes import loads_json_object

OLD = "2000-01-01T00:00:00+00:00"
OLD_MTIME = 946_684_800.0
EntryPoint = Literal["doctor", "runner-history", "history-archive"]


@contextmanager
def _old_active_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    held: bool,
) -> Iterator[tuple[Path, Path, str]]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid = "destructive-status"\n')
    (project / "mars.toml").write_text("[settings]\n")
    (project / ".mars" / "agents").mkdir(parents=True)
    (project / ".mars" / "skills").mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setattr(
        diag,
        "check_upgrade_availability",
        lambda *_args, **_kwargs: mars_ops.UpgradeAvailability(),
    )

    root = resolve_project_runtime_root_for_write(project)
    root.mkdir(parents=True, exist_ok=True)
    native_store = tmp_path / "native"
    native_store.mkdir()
    native_id = "00000001-1111-4111-8111-111111111111"
    (native_store / f"{native_id}.jsonl").write_text(
        json.dumps({"sessionId": native_id})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": "done"}})
        + "\n"
    )
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=native_id,
        native_store=str(native_store),
        model="test",
        chat_id="c1",
        spawn_id="p1",
    )
    session_store.stop_session(root, "c1")
    sessions_path = root / "sessions.jsonl"
    session_events = [
        loads_json_object(line) for line in sessions_path.read_text().splitlines()
    ]
    for event in session_events:
        if "started_at" in event:
            event["started_at"] = OLD
        if "stopped_at" in event:
            event["stopped_at"] = OLD
    sessions_path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in session_events)
    )

    with LaunchSlot() as launch:
        spawn_id = str(
            spawn_store.start_spawn(
                root,
                spawn_id="p1",
                chat_id="c1",
                owner_chat_id="c0",
                model="test",
                agent="coder",
                harness="claude",
                prompt="old active spawn",
                harness_session_id=native_id,
                started_at=OLD,
                launch_lock=launch,
            )
        )
        spawn_store.record_spawn_exited(root, spawn_id, exit_code=1, exited_at=OLD)
        spawn_dir = root / "spawns" / spawn_id
        (spawn_dir / "history.jsonl").write_text('{"event":"legacy"}\n')
        (spawn_dir / "report.md").write_text("possibly useful\n")
        state = spawn_store.get_spawn(root, spawn_id)
        assert state is not None
        raw_record = json.dumps({"type": "assistant", "message": {"content": "done"}})
        snapshot_header = SnapshotHeader(
            transcript=transcript_header(state, root.name),
            session_instance_id=state.session_instance_id,
            harness="claude",
            native_session_id=native_id,
            dialect="claude.fixture.v1",
            scope="fixture",
            observed_from=OLD,
        )
        snapshot_observation = SnapshotObservation(
            observed_until=OLD,
            sources=(
                SourceRevision(
                    source="claude",
                    sha256=hashlib.sha256(raw_record.encode()).hexdigest(),
                    records=1,
                ),
            ),
        )
        with (spawn_dir / NATIVE_SNAPSHOT_FILENAME).open("wb") as snapshot:
            write_snapshot(
                snapshot,
                snapshot_header,
                (SnapshotRecord(source="claude", ordinal=0, raw=raw_record),),
                lambda: snapshot_observation,
            )
        for path in (spawn_dir, *spawn_dir.rglob("*")):
            os.utime(path, (OLD_MTIME, OLD_MTIME))
        if not held:
            launch.close()
        yield project, root, spawn_id


def _spawn_bytes(root: Path, spawn_id: str) -> dict[str, bytes]:
    spawn_dir = root / "spawns" / spawn_id
    return {
        path.relative_to(spawn_dir).as_posix(): path.read_bytes()
        for path in spawn_dir.rglob("*")
        if path.is_file()
    }


def _run_entry_point(
    entry_point: EntryPoint,
    project: Path,
    tmp_path: Path,
) -> DoctorOutput | SessionArchiveOutput:
    if entry_point == "doctor":
        return doctor_sync(DoctorInput(project_root=str(project), prune=True))
    if entry_point == "runner-history":
        return session_archive_sync(
            SessionArchiveInput(
                project_root=str(project),
                prune_runner_history=True,
                apply=True,
                after_days=0,
            )
        )
    return session_archive_sync(
        SessionArchiveInput(
            project_root=str(project),
            destination=str(tmp_path / "archives"),
            eligible=True,
            apply=True,
            after_days=0,
        )
    )


@pytest.mark.parametrize("entry_point", ["doctor", "runner-history", "history-archive"])
def test_old_lock_held_spawn_survives_destructive_entry_point_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: EntryPoint,
) -> None:
    with _old_active_spawn(tmp_path, monkeypatch, held=True) as (project, root, spawn_id):
        before = _spawn_bytes(root, spawn_id)

        result = _run_entry_point(entry_point, project, tmp_path)

        assert _spawn_bytes(root, spawn_id) == before
        if entry_point == "doctor":
            assert isinstance(result, DoctorOutput)
            assert result.pruned_spawn_artifacts == 0
        elif entry_point == "runner-history":
            assert isinstance(result, SessionArchiveOutput)
            assert result.runner_history is not None
            assert result.runner_history.skipped == {"running": (spawn_id,)}
        else:
            assert isinstance(result, SessionArchiveOutput)
            assert result.selected == ()
            assert result.reclaimed == ()
            assert spawn_id in result.protected


@pytest.mark.parametrize("entry_point", ["doctor", "runner-history", "history-archive"])
def test_old_free_lock_stored_active_row_uses_each_destructive_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: EntryPoint,
) -> None:
    with _old_active_spawn(tmp_path, monkeypatch, held=False) as (project, root, spawn_id):
        before = _spawn_bytes(root, spawn_id)
        state_before = before["state.json"]

        result = _run_entry_point(entry_point, project, tmp_path)

        after = _spawn_bytes(root, spawn_id)
        assert after["state.json"] == state_before
        if entry_point == "doctor":
            assert isinstance(result, DoctorOutput)
            # Doctor protects every stored-active row from whole-directory pruning.
            assert result.pruned_spawn_artifacts == 0
            assert after == before
        elif entry_point == "runner-history":
            assert isinstance(result, SessionArchiveOutput)
            # Runner-history pruning uses derived liveness and may remove only its
            # redundant legacy streams; it never captures or retires the spawn row.
            assert result.runner_history is not None
            assert [row.spawn_id for row in result.runner_history.pruned] == [spawn_id]
            assert set(before) - set(after) == {"history.jsonl"}
        else:
            assert isinstance(result, SessionArchiveOutput)
            # Archive selection is destructive and therefore requires stored-terminal.
            assert result.selected == ()
            assert result.reclaimed == ()
            assert spawn_id in result.protected
            assert after == before
            with pytest.raises(ValueError, match="terminal record"):
                materialize_native_history(project, root, spawn_id)
            assert not list((tmp_path / "archives").glob("*.zip"))
